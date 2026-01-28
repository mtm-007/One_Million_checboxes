import modal
import subprocess
import polars as pl
import os, sys
from datetime import datetime

def export_to_parquet_polars():
    app_name = os.environ.get("APP_NAME")
    if not app_name:
        print("Error: APP_NAME environment variable not found.")
        print("Usage: APP_NAME=your-app-name python script.py")
        sys.exit(1)
    
    client = modal.Client.from_env()

    datestr = datetime.now().strftime("%Y%m%d_%H%M")
    output_file = f"modal_logs_{app_name}_{datestr}.parquet"

    print(f"📥 Fetching all logs for app: {app_name}")
    print("⚠️  Note: This will stream logs until the process completes or is interrupted.")
    

    logs_list = []
    count = 0

    try: 
        #app = modal.App.from_id(app_name, client=client)
        process = subprocess.Popen(
            [sys.executable, "-m", "modal", "app", "logs", app_name, "--timestamps"],
            stdout= subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )

        print("Streaming logs (press Ctrl + C to stop)")

        for line in process.stdout:
            line = line.strip()
            if not line: continue

            parts = line.split(maxsplit=1)

            if len(parts) >= 2:
                timestamp_str = parts[0]
                message = parts[1] if len(parts) > 1 else ""
            else:
                timestamp_str = datetime.now().isoformat()
                message = line
        
            logs_list.append({
                "timestamp": timestamp_str,
                "message": message,
                "source": "modal_app"
            })
            count += 1

            if count % 500 == 0:
                print(f"collected {count} lines...")
        
        return_code = process.wait()

        if return_code != 0:
            stderr = process.stderr.read()
            print(f"Modal CLI returned error code {return_code}")
            if stderr:
                print(f"Error output: {stderr}")

        if not logs_list:
            print(f"No logs found for App ID: {app_name}")
            return
        
        df = pl.DataFrame(logs_list)

        #write to parquet using ZSTD (best for log text)
        df.write_parquet(
            output_file,
            compression="zstd",
            compression_level=3,
            use_pyarrow=True
        )
        final_size = os.path.getsize(output_file) / (1024 * 1024)
        print(f"\n Export completed")
        print(f"Total Lines: {len(df)}")
        print(f"File: {output_file} ({final_size:.2f} MB)")
            
    except KeyboardInterrupt:
        print("\n Interupted by user")
        if logs_list:
            #save what we have so far
            df = pl.DataFrame(logs_list)
            df.write_parquet(
                output_file,
                compression="zstd",
                compression_level=3,
                use_pyarrow=True
            )
            final_size = os.path.getsize(output_file) / (1024 * 1024)
            print(f"\n Partial export saved")
            print(f"Lines captured: {len(df)}")
            print(f"File: {output_file} ({final_size:.2f} MB)")
        else:
            print("No logs caputured before interuption")

    except FileNotFoundError:
        print("❌ Error: 'modal' CLI not found.")
        print("   Please install Modal CLI: pip install modal")
        print("   And authenticate: modal token set")
        sys.exit(1)
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    print("=" * 60)
    print("Modal App Logs Exporter")
    print("=" * 60)
    export_to_parquet_polars()

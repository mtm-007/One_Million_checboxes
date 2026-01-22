#!/usr/bin/env python3
"""
Modal App Logs → Parquet exporter (using App ID + 5-second progress)
"""

import os
import sys
import subprocess
from datetime import datetime
import polars as pl
import pyarrow as pa
import signal
import time
import traceback

# Global flag for clean interrupt handling
interrupted = False

def signal_handler(sig, frame):
    global interrupted
    interrupted = True
    print("\n\nInterrupt received — finishing current batch and saving partial results...")

signal.signal(signal.SIGINT, signal_handler)


def export_to_parquet_polars():
    app_id = os.environ.get("APP_ID")
    if not app_id:
        print("Error: APP_ID environment variable not set.")
        print("Usage example:")
        print("  APP_ID=ap-hvpLl4nRQym4UIlgt0RQiS python this_script.py")
        print("   (or use uv run as before)")
        sys.exit(1)

    datestr = datetime.now().strftime("%Y%m%d_%H%M")
    output_file = f"modal_logs_{app_id}_{datestr}.parquet"

    print(f"📥 Exporting logs for App ID: {app_id}")
    print(f"Output will be saved to: {output_file}")
    print("Streaming logs (Ctrl+C to stop and save partial result)\n")

    # PyArrow schema
    schema = pa.schema([
        ("timestamp", pa.string()),
        ("message",   pa.string()),
        ("source",    pa.string()),
    ])

    batches = []
    batch_size = 20_000          # tune: 5k–50k depending on RAM / log volume
    rows = []
    count = 0
    last_report = time.time()    # start timer for progress reports

    try:
        # Launch modal CLI logs with timestamps — using APP_ID
        process = subprocess.Popen(
            [sys.executable, "-m", "modal", "app", "logs", app_id, "--timestamps"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            universal_newlines=True
        )

        for line in process.stdout:
            if interrupted:
                break

            line = line.strip()
            if not line:
                continue

            parts = line.split(maxsplit=1)
            if len(parts) >= 2:
                ts_str = parts[0]
                msg = parts[1]
            else:
                ts_str = datetime.now().isoformat()
                msg = line

            rows.append((ts_str, msg, "modal_app"))
            count += 1

            # Time-based progress (every 5 seconds)
            now = time.time()
            if now - last_report >= 5:
                preview = msg[:60] + "..." if len(msg) > 60 else msg
                print(f"  → {count:,} lines so far  (last: {preview})")
                last_report = now

            if len(rows) >= batch_size:
                batch = pa.RecordBatch.from_arrays(
                    [pa.array(col) for col in zip(*rows)],
                    schema=schema
                )
                batches.append(batch)
                rows.clear()

        # Flush remaining rows after stream ends
        if rows:
            batch = pa.RecordBatch.from_arrays(
                [pa.array(col) for col in zip(*rows)],
                schema=schema
            )
            batches.append(batch)
            count += len(rows)

        # Wait for process (catch errors)
        return_code = process.wait()

        if return_code != 0:
            stderr_output = process.stderr.read()
            print(f"Modal CLI failed (code {return_code})")
            if stderr_output.strip():
                print("Error output:\n", stderr_output)
            if not batches:
                print("No logs were captured.")
                return

        # ─── Combine & write Parquet ───
        if batches:
            table = pa.Table.from_batches(batches)
            df = pl.from_arrow(table)

            df.write_parquet(
                output_file,
                compression="zstd",
                compression_level=2,
                row_group_size=8 * 1024 * 1024,
                use_pyarrow=False
            )

            file_mb = os.path.getsize(output_file) / (1024 * 1024)
            print(f"\nExport completed")
            print(f"  Total lines: {count:,}")
            print(f"  File: {output_file}  ({file_mb:.2f} MB)")
        else:
            print("\nNo logs found / nothing captured.")
            print("Tip: Check Modal dashboard → your app → Logs tab to confirm if any logs exist.")
            print("     The CLI only shows recent/live logs; very old ones may not appear here.")

    except KeyboardInterrupt:
        print("\nInterrupted by user — saving partial result...")
    except FileNotFoundError:
        print("❌ 'modal' CLI not found.")
        print("   Install:  pip install modal")
        print("   Auth:     modal token set")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        traceback.print_exc()
        sys.exit(1)

    # Save partial result if interrupted
    finally:
        if interrupted and rows:
            batch = pa.RecordBatch.from_arrays(
                [pa.array(col) for col in zip(*rows)],
                schema=schema
            )
            batches.append(batch)

        if interrupted and batches:
            try:
                table = pa.Table.from_batches(batches)
                df = pl.from_arrow(table)
                partial_file = output_file.replace(".parquet", "_partial.parquet")
                df.write_parquet(
                    partial_file,
                    compression="zstd",
                    compression_level=2,
                    row_group_size=4 * 1024 * 1024
                )
                file_mb = os.path.getsize(partial_file) / (1024 * 1024)
                print(f"Partial result saved to: {partial_file} ({file_mb:.2f} MB)")
                print(f"Lines captured: {count:,}")
            except Exception as e:
                print(f"Failed to save partial result: {e}")

        if process.poll() is None:
            try:
                process.terminate()
            except:
                pass


if __name__ == "__main__":
    print("═" * 70)
    print("  Modal Logs → Parquet  (App ID version + 5-second progress)")
    print("═" * 70)
    export_to_parquet_polars()
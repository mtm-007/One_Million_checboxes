#!/usr/bin/env python3
"""
Script to fetch logs from a Modal deployed web app and save them to a file.
"""

import subprocess
import sys
from datetime import datetime
import argparse

def fetch_modal_logs(app_name=None, output_file=None, lines=100, follow=False, get_all=False):
    """
    Fetch logs from Modal app and save to file.
    
    Args:
        app_name: Name of the Modal app (optional, will use current app if not specified)
        output_file: Path to save logs (default: modal_logs_TIMESTAMP.txt)
        lines: Number of log lines to fetch (default: 100)
        follow: If True, continuously stream logs (default: False)
        get_all: If True, fetch all available logs (default: False)
    """
    
    # Generate default output filename with timestamp
    if output_file is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = f"modal_logs_{timestamp}.txt"
    
    print(f"Fetching logs from Modal app...")
    if app_name:
        print(f"App: {app_name}")
    print(f"Output file: {output_file}")
    print("-" * 50)
    
    try:
        # Build the modal logs command
        cmd = ["modal", "app", "logs"]
        
        if app_name:
            cmd.append(app_name)
        
        if not follow:
            # Use a very large number to get all logs if --all is specified
            log_lines = 999999 if get_all else lines
            cmd.extend(["--lines", str(log_lines)])
        
        if follow:
            cmd.append("--follow")
            print("Streaming logs (press Ctrl+C to stop)...")
        
        # Execute the command and capture output
        if follow:
            # For follow mode, stream to file in real-time
            with open(output_file, 'w') as f:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
                
                try:
                    for line in process.stdout:
                        print(line, end='')  # Print to console
                        f.write(line)  # Write to file
                        f.flush()  # Ensure it's written immediately
                except KeyboardInterrupt:
                    print("\n\nStopped streaming logs.")
                    process.terminate()
        else:
            # For non-follow mode, get all logs at once
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True
            )
            
            # Save logs to file
            with open(output_file, 'w') as f:
                f.write(result.stdout)
                if result.stderr:
                    f.write("\n--- STDERR ---\n")
                    f.write(result.stderr)
            
            print(f"\n✓ Logs saved to: {output_file}")
            print(f"Total lines: {len(result.stdout.splitlines())}")
            
            # Show preview of logs
            lines = result.stdout.splitlines()
            if lines:
                print("\nPreview (first 10 lines):")
                print("-" * 50)
                for line in lines[:10]:
                    print(line)
                if len(lines) > 10:
                    print(f"... ({len(lines) - 10} more lines)")
        
        return output_file
        
    except subprocess.CalledProcessError as e:
        print(f"\n❌ Error fetching logs: {e}", file=sys.stderr)
        if e.stderr:
            print(f"Error details: {e.stderr}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print("\n❌ Error: 'modal' command not found.", file=sys.stderr)
        print("Please install Modal CLI: pip install modal", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Fetch logs from Modal deployed web app and save to file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Fetch last 100 lines from current app
  python fetch_modal_logs.py
  
  # Fetch ALL available logs
  python fetch_modal_logs.py --all
  
  # Fetch logs from specific app
  python fetch_modal_logs.py --app my-web-app
  
  # Fetch more lines
  python fetch_modal_logs.py --lines 500
  
  # Save to specific file
  python fetch_modal_logs.py --output my_logs.txt
  
  # Stream logs in real-time
  python fetch_modal_logs.py --follow
        """
    )
    
    parser.add_argument(
        "--app", "-a",
        help="Name of the Modal app (optional)"
    )
    parser.add_argument(
        "--output", "-o",
        help="Output file path (default: modal_logs_TIMESTAMP.txt)"
    )
    parser.add_argument(
        "--lines", "-n",
        type=int,
        default=100,
        help="Number of log lines to fetch (default: 100)"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Fetch all available logs (ignores --lines)"
    )
    parser.add_argument(
        "--follow", "-f",
        action="store_true",
        help="Stream logs in real-time"
    )
    
    args = parser.parse_args()
    
    fetch_modal_logs(
        app_name=args.app,
        output_file=args.output,
        lines=args.lines,
        follow=args.follow,
        get_all=args.all
    )


if __name__ == "__main__":
    main()
import subprocess
import sys
import time
import os

# Set the working directory to the aider project root
os.chdir(r"C:\Users\PDitty\Documents\MEGA\code\aider-ce")

aider_process = subprocess.Popen(
    [
        "C:\\Users\\PDitty\\Documents\\MEGA\\code\\aider-ce\\venv_laptop\\Scripts\\python.exe",
        "-m",
        "aider",
        "--yes",
    ],
    stdin=subprocess.PIPE,
    stdout=sys.stdout,
    stderr=sys.stderr,
    text=True,
    bufsize=1,
    encoding='utf-8',
)

print("Waiting for aider to start...", flush=True)
time.sleep(10)

def run_command(command, wait_time=10):
    print(f"--- Running command: {command} ---", flush=True)
    if aider_process.poll() is not None:
        print("Aider process terminated unexpectedly.", flush=True)
        return False
    aider_process.stdin.write(command + "\n")
    aider_process.stdin.flush()
    time.sleep(wait_time)
    return True

if not run_command("/agent"):
    sys.exit(1)

if not run_command("/tools"):
    sys.exit(1)

if not run_command("/tools-create my_tool.py 'this is a test tool'"):
    sys.exit(1)

if not run_command("/tools"):
    sys.exit(1)

# The tool should be created in .aider/custom_tools/my_tool.py
tool_path = ".aider/custom_tools/my_tool.py"

if not run_command(f"/tools-load {tool_path}"):
    sys.exit(1)

if not run_command(f"/tools-fix {tool_path}"):
    sys.exit(1)

fix_prompt = f"Fix the tool in `{tool_path}`. It should be a simple hello world tool."
if not run_command(fix_prompt, wait_time=30):
    sys.exit(1)

if not run_command("/tools-move my_tool global"):
    sys.exit(1)

if not run_command("/tools-unload my_tool"):
    sys.exit(1)

if not run_command("/tools"):
    sys.exit(1)

if not run_command("/exit"):
    sys.exit(1)


if aider_process.poll() is None:
    print("--- Closing aider ---", flush=True)
    aider_process.stdin.close()
    aider_process.wait()
else:
    print(f"Aider process exited with code {aider_process.returncode}", flush=True)

print("--- Script finished ---", flush=True)

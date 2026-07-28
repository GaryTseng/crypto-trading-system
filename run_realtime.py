import subprocess
import sys

print("Starting realtime wrapper...", flush=True)

# Run the simulation and stream output line by line
process = subprocess.Popen(
    ["python", "-u", "simulate_7days_fast.py"],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    encoding="utf-8",
    bufsize=1
)

for line in iter(process.stdout.readline, ""):
    sys.stdout.write(line)
    sys.stdout.flush()

process.stdout.close()
return_code = process.wait()
print(f"\nWrapper completed with return code: {return_code}", flush=True)

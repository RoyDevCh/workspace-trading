import subprocess
result = subprocess.run(
    ["python", "test_executor2.py"],
    cwd="C:/Users/Roy/.openclaw/workspace-trading",
    capture_output=True, text=True, timeout=15
)
print(result.stdout)
if result.stderr:
    print("STDERR:", result.stderr[:1000])

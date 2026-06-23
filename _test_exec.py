import sys
sys.path.insert(0, r"D:\myproject\container_036fa50daf44\src")
from isolation.executor import IsolationExecutor
e = IsolationExecutor()
print("created OK:", type(e).__name__)
print("docker_provider:", type(e._docker_provider).__name__)

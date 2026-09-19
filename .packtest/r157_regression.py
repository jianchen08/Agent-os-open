# -*- coding: utf-8 -*-
"""R157: 五路回归扫（WS协议）——主链+三模式注入，重启后最新 HEAD 复测。"""
import asyncio
import io
import json
import sys
import time
import urllib.request

sys.stdout = __import__("io").TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"D:\myproject\container_e17cc5927dfd\scripts\eval_bench")
from kernel_client import KernelClient, dispatch_and_collect

BASE = "http://localhost:9100"
TS = str(int(time.time()))


def api(method, ep, token):
    req = urllib.request.Request(BASE + ep, method=method,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=15))


async def main() -> int:
    kc = KernelClient(BASE)
    kc.login("admin", "admin12345")
    token = kc.token
    ws = kc.ws_chat_url()
    results = {}

    def sess(name):
        s = kc.create_session(name)
        return s["thread"]["id"] if isinstance(s.get("thread"), dict) else s.get("thread_id") or s.get("id")

    async def dispatch(tid, q, mid, mode=None):
        ec = {"mode": mode} if mode else None
        return await dispatch_and_collect(ws, tid, q, mid, timeout_s=150, execution_context=ec)

    # 1) 主链冒烟（无模式）
    t1 = sess("R157 冒烟")
    r1 = await dispatch(t1, "R157 冒烟：只回复两个字：正常", f"r157a-{TS}")
    results["冒烟"] = {"pipeline": r1.pipeline_id, "terminal": r1.terminal}

    # 2) 写作模式
    t2 = sess("R157 写作")
    r2 = await dispatch(t2, "R157 写作：只回复两个字：收到", f"r157b-{TS}", mode="writing")
    results["写作"] = {"pipeline": r2.pipeline_id, "terminal": r2.terminal}

    # 3) 调研模式
    t3 = sess("R157 调研")
    r3 = await dispatch(t3, "R157 调研：只回复两个字：OK", f"r157c-{TS}", mode="research")
    results["调研"] = {"pipeline": r3.pipeline_id, "terminal": r3.terminal}

    # 4) 编码模式
    t4 = sess("R157 编码")
    r4 = await dispatch(t4, "R157 编码：只回复两个字：完成", f"r157d-{TS}", mode="coding")
    results["编码"] = {"pipeline": r4.pipeline_id, "terminal": r4.terminal}

    print("== 结果 ==")
    for k, v in results.items():
        print(f"  {k}: pipeline={v['pipeline']} terminal={v['terminal']}")

    await asyncio.sleep(8)
    st = api("GET", "/api/v1/pipelines/state", token)
    items = st.get("data", st).get("items", [])
    want = {results["写作"]["pipeline"]: "writing", results["调研"]["pipeline"]: "research",
            results["编码"]["pipeline"]: "coding"}
    ok = 0
    for it in items:
        pid = it.get("pipeline_id", "")
        if pid in want:
            got = (it.get("state") or {}).get("mode", "")
            print(f"  {want[pid]}: state.mode={got} -> {'PASS' if got == want[pid] else 'FAIL'}")
            if got == want[pid]:
                ok += 1
    print(f"mode injection: {ok}/3")
    return 0 if ok == 3 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

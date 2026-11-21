# TB 评测轮运行手册（每轮环境还原程序）

> 配套台账：`docs/working/TB参赛适配_台账.md`。本手册固化「每轮评测跑前的
> 环境还原三步」（R19 运维固化）——目的：上一轮的内存/状态累积、僵尸容器、
> 工作区残留不得泄入下一轮（轮间唯一变量 = 被测题，不是环境）。
>
> 轮询预算口径（R19 起）：适配器自动读 trial 的 task.toml `[agent]
> timeout_sec`（评测基准自己的配置，与 harbor trial 执法同源）；**不要再用
> `AGENTOS_POLL_TIMEOUT_S` env 人为拉长**——它已降级为 task.toml 缺席时的
> 兜底（兜底链：task.toml > option > env > 1800s 缺省）。

## 每轮跑前固定三步

### ① respawn scratch 内核（清上一轮内存/状态累积）

内核每轮全新实例：旧实例的内存态（绑定注册表、管道状态、LLM 失败计数等）
随进程消亡，不复用。

```bash
# Windows 侧（仓根）
# 1a. 停旧实例（先 status 确认；不杀会占端口 + 旧状态泄入新轮）
python skills/skill-dev-toolkit/scripts/spawn_test_kernel.py status
python skills/skill-dev-toolkit/scripts/spawn_test_kernel.py stop

# 1b. spawn 新实例（宿主同款相对形态，R6 记录；记下输出的 port/password）
python skills/skill-dev-toolkit/scripts/spawn_test_kernel.py spawn \
  --port 9189 --password <口令> --config-root config
```

注意：动内核只能经 spawn_test_kernel / electron，**禁止 bash 直拉内核**
（仓规 AGENTS.md：孤儿内核缺密钥注入 + 占端口）。

### ② harbor trial 自然新容器

harbor 每 trial 自起全新 compose 容器（project name = sanitize(session_id)），
无需人工干预；只需保证上一轮容器已不在场（ trial 结束即由 harbor 收尾，
外部容器通道销毁面绝不 docker rm 收养容器——若上轮 trial 中途夭折遗留
容器，按容器名手工清理）：

```bash
# Ubuntu WSL 侧
docker ps --format '{{.Names}}' | grep -i <上轮题名>   # 应为空
# 残留时按名清（容器归 harbor 所有，残留即上轮已死，可安全删）
docker rm -f <残留容器名>
```

### ③ 确认任务工作区无前轮残留

工作空间每 trial 一个新目录（`tb-<uuid12>`）；确认 jobs 目录下本轮 trial
的 `agent/agentos-workspaces/` 只含本轮目录，且内核侧用户空间无上轮写入
面混入：

```bash
# Ubuntu WSL 侧（harbor jobs 目录，-o 参数所指）
ls jobs/<最新trial目录>/agent/agentos-workspaces/   # 只应有本轮 tb-* 目录
```

## 跑评测轮（参考形态，Ubuntu WSL 侧）

```bash
cd ~/tb-smoke && PYTHONPATH=/mnt/d/<repo> ~/tb-venv/bin/harbor run \
  -p ~/tb-smoke/tb41/terminal-bench/<task_name> \
  -a scripts.eval_bench.harbor_agentos.adapter:AgentOSHarborAgent \
  -n 1 -o jobs -y \
  -c '{"kernel_url": "http://<宿主IP>:9189"}'
```

口令经环境变量 `AGENTOS_ADMIN_PASSWORD` 进适配器（禁写进 -c / trajectory）。
轮询预算随该题 task.toml 自动对齐（进度日志每 60s 一条，写在 harbor
trial.log / 适配器日志里，超长预算不再静默）。

## 轮后清理

- scratch 内核 stop（①同款 stop 命令）；
- 探针/冒烟容器 rm（`docker ps` 只剩既有退出态为净）；
- 交付物以 harbor `results.json` + 适配器 metadata（task 域坐标全量）为准。

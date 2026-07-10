"""自动确认启动器 — 替换人类交互通知器为自动确认版本。

用法:
    cd "d:\\Jianguoyun\\Agent os"
    set PYTHONPATH=src
    python -m auto_confirm_runner --mode auto -m "你的消息"

功能:
    在 CLI 启动前注入 AutoConfirmNotifier，自动批准所有人类交互请求。
    用于自动化测试场景，无需人工干预。
"""

import asyncio
import logging
import threading
import time

logger = logging.getLogger(__name__)


class AutoConfirmNotifier:
    """自动确认所有人类交互请求的通知器。"""

    def __init__(self, confirm_delay: float = 1.0, default_feedback: str = "确认通过，请继续执行"):
        self._confirm_delay = confirm_delay
        self._default_feedback = default_feedback
        self._service = None
        self._confirmed_count = 0

    def set_service(self, service):
        self._service = service

    async def notify_request(self, request) -> bool:
        request_id = request.get("id") if isinstance(request, dict) else getattr(request, "id", "")
        title = ""
        if isinstance(request, dict):
            title = request.get("message_data", {}).get("title", "")
        logger.info("[AutoConfirm] 收到交互请求: id=%s, title=%s", request_id, title)

        if request_id and self._service:
            await asyncio.sleep(self._confirm_delay)
            msg_data = {}
            if isinstance(request, dict):
                msg_data = request.get("message_data", {})
            mode = msg_data.get("interaction_mode", "choice")

            if mode == "conversation":
                await self._service.submit_response(
                    request_id=request_id,
                    response_type="answered",
                    answers=["确认，请按照你的方案继续执行，不需要进一步澄清。"],
                    feedback=self._default_feedback,
                )
            else:
                options = msg_data.get("options", [])
                first_option_id = options[0]["id"] if options else "approve"
                await self._service.submit_response(
                    request_id=request_id,
                    response_type="approved",
                    selected_option=first_option_id,
                    feedback=self._default_feedback,
                )
            self._confirmed_count += 1
            logger.info("[AutoConfirm] 已自动确认请求 #%d: %s", self._confirmed_count, request_id)
        return True

    async def notify_cancel(self, request_id: str, reason=None, thread_id: str = "") -> bool:
        logger.info("[AutoConfirm] 请求已取消: %s", request_id)
        return True

    async def notify_timeout(self, request_id: str, thread_id: str = "") -> bool:
        logger.info("[AutoConfirm] 请求已超时: %s", request_id)
        return True

    async def notify_timeout_reminder(self, request_id, remaining_seconds, thread_id="", **kw) -> bool:
        return True

    async def notify_conversation_start(self, thread_id, tab_id, title, **kw) -> bool:
        logger.info("[AutoConfirm] 对话模式启动: %s", title)
        return True


def inject_auto_confirm():
    """注入自动确认通知器到全局人类交互服务。"""
    from human_interaction import get_human_interaction_service  # noqa: PLC0415

    notifier = AutoConfirmNotifier(confirm_delay=1.5)
    human_svc = get_human_interaction_service()
    notifier.set_service(human_svc)
    human_svc.set_notifier(notifier)
    logger.info("[AutoConfirm] 已注入 AutoConfirmNotifier")


def _background_inject():
    """后台线程持续尝试注入，直到成功。"""
    for attempt in range(30):
        try:
            from human_interaction import get_human_interaction_service  # noqa: PLC0415

            svc = get_human_interaction_service()
            if svc is not None:
                notifier = AutoConfirmNotifier(confirm_delay=1.5)
                notifier.set_service(svc)
                svc.set_notifier(notifier)
                logger.info("[AutoConfirm] 后台注入成功 (attempt %d)", attempt + 1)
                return
        except Exception as exc:
            logger.debug(
                "[AutoConfirm] 后台注入尝试 %d 失败: %s",
                attempt + 1,
                exc,
            )
        time.sleep(0.5)


def main():
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="Agent OS CLI (AutoConfirm)")
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--no-streaming", action="store_true")
    parser.add_argument("--mode", type=str, default="auto", choices=["normal", "auto", "plan"])
    parser.add_argument("--message", "-m", type=str, default=None)
    args = parser.parse_args()

    inject_thread = threading.Thread(target=_background_inject, daemon=True)
    inject_thread.start()

    from channels.cli.cli_main import CLIApplication, setup_logging  # noqa: PLC0415

    setup_logging(debug=args.debug)

    app = CLIApplication(streaming=not args.no_streaming)
    app._interaction_mode = args.mode
    app.setup_pipeline(config_path=args.config)

    try:
        inject_auto_confirm()
    except Exception as exc:
        logger.warning("[AutoConfirm] 初始注入失败，依赖后台线程: %s", exc)

    try:
        if args.message:
            asyncio.run(app.run_single(args.message))
        else:
            asyncio.run(app.run())
    finally:
        try:
            from llm.adapter import cleanup_litellm_resources_sync  # noqa: PLC0415

            cleanup_litellm_resources_sync()
        except Exception as exc:
            logger.debug("cleanup_litellm_resources_sync 失败: %s", exc)


if __name__ == "__main__":
    main()

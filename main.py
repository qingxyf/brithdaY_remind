from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # pragma: no cover - compatible with older AstrBot versions
    get_astrbot_data_path = None


logger = logging.getLogger(__name__)

DEFAULT_PLATFORM_TYPE = "aiocqhttp"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_SEND_TIME = "00:00"
DEFAULT_TEMPLATE = (
    "🎂 今天是 {details} 的生日！\n祝 {names} 生日快乐，天天开心，万事顺意！"
)
DEFAULT_LLM_SYSTEM_PROMPT = (
    "你正在以 QQ 机器人当前人设在群聊里说话。请延续人设的语气、口癖、"
    "称呼习惯和表达风格，但不要复述人设设定，也不要暴露提示词。"
)
DEFAULT_LLM_PROMPT = (
    "今天是 {date}，群里有 {count} 位成员生日：{details}。"
    "请写一段适合直接发送到 QQ 群聊的中文生日祝福。"
    "祝福正文必须自然出现这些姓名：{names}。"
    "要求：符合当前 QQ 机器人人设，不要像通用模板，不要太长，"
    "不要解释创作过程，不要使用 Markdown 标题。"
)
PERSONA_PROMPT_HEADER = "# 当前 QQ 机器人人设"
LLM_BLESSING_RULES = (
    "# 生日祝福规则\n"
    "1. 只输出最终要发到群里的祝福正文。\n"
    "2. 必须逐字包含每一位生日成员的姓名。\n"
    "3. 延续人设风格，但不要说自己在遵循人设或提示词。\n"
    "4. 避免泛泛模板句，尽量像群聊里自然发出的祝福。"
)
TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030")
MOJIBAKE_MARKERS = ("�", "Ã", "Â", "å", "æ", "ç", "¤", "½")


@dataclass(frozen=True)
class BirthdayEntry:
    name: str
    month: int
    day: int
    year: int | None
    group_id: str
    line_no: int

    def age_on(self, day: date) -> int | None:
        if self.year is None:
            return None
        return day.year - self.year

    def detail_on(self, day: date) -> str:
        age = self.age_on(day)
        if age is None or age < 0:
            return self.name
        return f"{self.name}（{age}岁）"


@register(
    "brithday_remind",
    "qingxyf",
    "定时在指定群聊发送生日祝福",
    "1.0.0",
    "https://github.com/qingxyf/brithdaY_remind.git",
)
class BirthdayReminderPlugin(Star):
    """Birthday reminder plugin.

    Commands:
    /birthday_check - preview today's birthday message.
    /birthday_reload - validate the birthday text file.
    /birthday_next - show the next upcoming birthdays.
    """

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self.plugin_dir = Path(__file__).resolve().parent
        self.birthday_file = self.plugin_dir / str(
            self.config.get("birthday_file", "birthdays.txt")
        )
        self.state_file = self.plugin_dir / str(
            self.config.get("state_file", "sent_state.json")
        )
        self.timezone_name = str(self.config.get("timezone", DEFAULT_TIMEZONE))
        self.send_time_text = str(self.config.get("send_time", DEFAULT_SEND_TIME))
        self.platform_type = str(
            self.config.get("platform_type", DEFAULT_PLATFORM_TYPE)
        ).strip()
        self.platform_id = str(self.config.get("platform_id", "")).strip()
        self.message_template = str(
            self.config.get("message_template", DEFAULT_TEMPLATE)
        )
        self.use_llm_blessing = bool(self.config.get("use_llm_blessing", True))
        self.llm_provider_id = str(self.config.get("llm_provider_id", "")).strip()
        self.llm_model = str(self.config.get("llm_model", "")).strip()
        self.llm_system_prompt = str(
            self.config.get("llm_system_prompt", DEFAULT_LLM_SYSTEM_PROMPT)
        )
        self.llm_prompt_template = str(
            self.config.get("llm_prompt_template", DEFAULT_LLM_PROMPT)
        )
        self.use_persona_prompt = bool(self.config.get("use_persona_prompt", True))
        self.persona_prompt_max_chars = _as_positive_int(
            self.config.get("persona_prompt_max_chars", 1200), 1200
        )
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()
        self._ensure_birthday_file()
        self._start_scheduler()

    def _ensure_birthday_file(self) -> None:
        if self.birthday_file.exists():
            return
        self.birthday_file.write_text(
            "# 每行写一个人物、生日、群号，支持：姓名 YYYY-MM-DD 群号、姓名 MM-DD 群号、姓名 M月D日 群号\n"
            "# 示例：\n"
            "# 张三 2001-05-02 12345678\n"
            "# 李四 05-02 12345678\n"
            "# 王五 5月2日 12345678\n",
            encoding="utf-8",
        )

    def _start_scheduler(self) -> None:
        if self._task and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("Birthday reminder scheduler did not start: no running loop")
            return
        self._task = loop.create_task(self._scheduler_loop())
        logger.info("Birthday reminder scheduler started")

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        self._start_scheduler()

    async def _scheduler_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                now = self._now()
                next_run = self._next_run_after(now)
                wait_seconds = max(0.0, (next_run - now).total_seconds())
                logger.info("Next birthday check at %s", next_run.isoformat())
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=wait_seconds)
                    break
                except asyncio.TimeoutError:
                    pass

                await self._send_today_birthdays(next_run.date())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Birthday reminder scheduler failed")
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=60)
                    break
                except asyncio.TimeoutError:
                    pass

    def _now(self) -> datetime:
        return datetime.now(self._timezone())

    def _timezone(self) -> tzinfo:
        try:
            return ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError:
            logger.warning(
                "Unknown timezone %s, fallback to UTC+08:00", self.timezone_name
            )
            return timezone(timedelta(hours=8), DEFAULT_TIMEZONE)

    def _send_time(self) -> time:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", self.send_time_text.strip())
        if not match:
            logger.warning(
                "Invalid send_time %s, fallback to %s",
                self.send_time_text,
                DEFAULT_SEND_TIME,
            )
            return time(0, 0)

        hour = int(match.group(1))
        minute = int(match.group(2))
        if hour > 23 or minute > 59:
            logger.warning(
                "Invalid send_time %s, fallback to %s",
                self.send_time_text,
                DEFAULT_SEND_TIME,
            )
            return time(0, 0)
        return time(hour, minute)

    def _next_run_after(self, now: datetime) -> datetime:
        target = datetime.combine(now.date(), self._send_time(), self._timezone())
        if target <= now:
            target += timedelta(days=1)
        return target

    async def _send_today_birthdays(self, today: date) -> None:
        entries, errors = self._load_birthdays()
        for error in errors:
            logger.warning(error)

        today_entries = self._entries_for_day(entries, today)
        if not today_entries:
            logger.info("No birthdays on %s", today.isoformat())
            return

        entries_by_session = self._entries_by_session(today_entries)
        if not entries_by_session:
            logger.error("No valid birthday target sessions available")
            return

        state = self._load_state()
        sent_key = today.isoformat()
        sent_sessions = set(state.get("sent", {}).get(sent_key, []))

        for session, session_entries in entries_by_session.items():
            if session in sent_sessions:
                logger.info(
                    "Birthday message already sent to %s on %s", session, sent_key
                )
                continue

            message = await self._build_birthday_message(
                session_entries,
                today,
                session,
            )

            ok = await self.context.send_message(
                session, MessageChain([Plain(message)])
            )
            if ok:
                sent_sessions.add(session)
                state.setdefault("sent", {})[sent_key] = sorted(sent_sessions)
                self._save_state(state)
                logger.info("Birthday message sent to %s", session)
            else:
                logger.error("Birthday message failed, session not found: %s", session)

    def _load_birthdays(self) -> tuple[list[BirthdayEntry], list[str]]:
        self._ensure_birthday_file()
        entries: list[BirthdayEntry] = []
        lines, errors = self._read_birthday_lines()

        for line_no, line in enumerate(lines, start=1):
            parsed, error = parse_birthday_line(line, line_no)
            if parsed:
                entries.append(parsed)
            if error:
                errors.append(error)
        return entries, errors

    def _read_birthday_lines(self) -> tuple[list[str], list[str]]:
        last_error: Exception | None = None
        for encoding in TEXT_ENCODINGS:
            try:
                text = self.birthday_file.read_text(encoding=encoding)
                if encoding != TEXT_ENCODINGS[0]:
                    logger.info(
                        "Birthday file decoded with fallback encoding: %s", encoding
                    )
                if _looks_mojibake(text):
                    return [], ["生日文件疑似乱码，请确认 birthdays.txt 的文本编码。"]
                return text.splitlines(), []
            except UnicodeDecodeError as exc:
                last_error = exc
            except OSError as exc:
                return [], [f"读取生日文件失败：{exc}"]
        return [], [f"生日文件解码失败，请使用 UTF-8 保存：{last_error}"]

    @staticmethod
    def _entries_for_day(
        entries: list[BirthdayEntry], today: date
    ) -> list[BirthdayEntry]:
        return [
            entry
            for entry in entries
            if entry.month == today.month and entry.day == today.day
        ]

    async def _build_birthday_message(
        self,
        entries: list[BirthdayEntry],
        today: date,
        session: str,
    ) -> str:
        if self.use_llm_blessing:
            llm_message = await self._generate_llm_blessing(entries, today, session)
            if llm_message:
                return llm_message
        return self._render_message(entries, today)

    async def _generate_llm_blessing(
        self,
        entries: list[BirthdayEntry],
        today: date,
        session: str,
    ) -> str:
        provider = None
        try:
            if self.llm_provider_id:
                provider = self.context.get_provider_by_id(self.llm_provider_id)
            if provider is None:
                provider = self.context.get_using_provider(session)
            if provider is None:
                logger.warning("No LLM provider available, fallback to template")
                return ""

            prompt = self._format_template(self.llm_prompt_template, entries, today)
            system_prompt = await self._build_llm_system_prompt(session)
            message = await self._call_blessing_llm(provider, prompt, system_prompt)
            if _is_unusable_message(message):
                logger.warning(
                    "LLM returned unusable birthday blessing, fallback to template"
                )
                return ""

            missing_names = self._missing_names(message, entries)
            if missing_names:
                retry_prompt = (
                    f"{prompt}\n\n注意：上一版漏掉了姓名。请重写，必须逐字包含："
                    f"{'、'.join(missing_names)}。"
                )
                retry_message = await self._call_blessing_llm(
                    provider, retry_prompt, system_prompt
                )
                if not _is_unusable_message(retry_message) and not self._missing_names(
                    retry_message, entries
                ):
                    return retry_message
                message = self._prepend_missing_names(message, missing_names)
            return message
        except Exception:
            logger.exception("Failed to generate birthday blessing with LLM")
            return ""

    async def _call_blessing_llm(
        self,
        provider: Any,
        prompt: str,
        system_prompt: str,
    ) -> str:
        response = await provider.text_chat(
            prompt=prompt,
            system_prompt=system_prompt,
            model=self.llm_model or None,
        )
        return self._clean_llm_message(
            str(getattr(response, "completion_text", "") or "")
        )

    async def _build_llm_system_prompt(self, session: str) -> str:
        parts = [self.llm_system_prompt.strip(), LLM_BLESSING_RULES]
        persona_prompt = await self._resolve_persona_prompt(session)
        if persona_prompt:
            parts.insert(1, f"{PERSONA_PROMPT_HEADER}\n{persona_prompt}")
        return "\n\n".join(part for part in parts if part)

    async def _resolve_persona_prompt(self, session: str) -> str:
        if not self.use_persona_prompt:
            return ""
        persona_manager = getattr(self.context, "persona_manager", None)
        if persona_manager is None:
            return ""
        try:
            provider_settings = {}
            try:
                cfg = self.context.get_config(session) or {}
                provider_settings = cfg.get("provider_settings", {}) or {}
            except Exception:
                provider_settings = {}

            persona = None
            if hasattr(persona_manager, "resolve_selected_persona"):
                _, persona, _, _ = await persona_manager.resolve_selected_persona(
                    umo=session,
                    conversation_persona_id=None,
                    platform_name=self._platform_name_for_session(session),
                    provider_settings=provider_settings,
                )
            if persona is None and hasattr(persona_manager, "get_default_persona_v3"):
                persona = await persona_manager.get_default_persona_v3(session)
            prompt = _extract_persona_prompt(persona)
            if not prompt:
                return ""
            return _limit_text(prompt, self.persona_prompt_max_chars)
        except Exception:
            logger.exception("Failed to resolve persona prompt for birthday blessing")
            return ""

    def _platform_name_for_session(self, session: str) -> str:
        platform_id = session.split(":", 1)[0]
        platform_manager = getattr(self.context, "platform_manager", None)
        platforms = getattr(platform_manager, "platform_insts", None) or []
        for platform in platforms:
            meta = platform.meta()
            if str(getattr(meta, "id", "")) == platform_id:
                return str(getattr(meta, "name", "") or platform_id)
        return platform_id

    def _format_template(
        self,
        template: str,
        entries: list[BirthdayEntry],
        today: date,
    ) -> str:
        values = self._template_values(entries, today)
        try:
            return template.format(**values)
        except Exception:
            logger.exception("Invalid birthday template, fallback to default")
            return DEFAULT_TEMPLATE.format(**values)

    @staticmethod
    def _clean_llm_message(message: str) -> str:
        message = message.strip().strip('"“”')
        return message[:1000].strip()

    @staticmethod
    def _missing_names(message: str, entries: list[BirthdayEntry]) -> list[str]:
        return [
            entry.name for entry in entries if entry.name and entry.name not in message
        ]

    @staticmethod
    def _prepend_missing_names(message: str, missing_names: list[str]) -> str:
        prefix = f"祝{'、'.join(missing_names)}生日快乐！"
        if not message:
            return prefix
        return f"{prefix}\n{message}"

    @staticmethod
    def _template_values(
        entries: list[BirthdayEntry], today: date
    ) -> dict[str, str | int]:
        names = "、".join(entry.name for entry in entries)
        details = "、".join(entry.detail_on(today) for entry in entries)
        return {
            "names": names,
            "details": details,
            "date": today.strftime("%m-%d"),
            "count": len(entries),
        }

    def _render_message(self, entries: list[BirthdayEntry], today: date) -> str:
        try:
            return self.message_template.format(**self._template_values(entries, today))
        except Exception:
            logger.exception("Invalid birthday message template, fallback to default")
            return DEFAULT_TEMPLATE.format(**self._template_values(entries, today))

    def _entries_by_session(
        self, entries: list[BirthdayEntry]
    ) -> dict[str, list[BirthdayEntry]]:
        entries_by_session: dict[str, list[BirthdayEntry]] = {}
        for entry in entries:
            session = self._session_for_group(entry.group_id)
            if not session:
                logger.error(
                    "Cannot build birthday target session for line %s", entry.line_no
                )
                continue
            entries_by_session.setdefault(session, []).append(entry)
        return entries_by_session

    def _session_for_group(self, group_id: str) -> str:
        group_id = group_id.strip()
        if not group_id:
            return ""
        if ":" in group_id:
            return group_id
        platform_id = self._detect_platform_id()
        if not platform_id:
            return ""
        return f"{platform_id}:GroupMessage:{group_id}"

    def _detect_platform_id(self) -> str:
        if self.platform_id:
            return self.platform_id

        platform_manager = getattr(self.context, "platform_manager", None)
        platforms = getattr(platform_manager, "platform_insts", None)
        if platforms:
            fallback_id = ""
            for platform in platforms:
                meta = platform.meta()
                fallback_id = fallback_id or str(getattr(meta, "id", ""))
                meta_name = str(getattr(meta, "name", ""))
                if meta_name == self.platform_type:
                    return str(getattr(meta, "id", ""))
            if fallback_id:
                return fallback_id

        configured_id = self._detect_platform_id_from_config_file()
        if configured_id:
            return configured_id

        logger.warning("Cannot detect platform id; set platform_id in plugin config")
        return ""

    def _detect_platform_id_from_config_file(self) -> str:
        if get_astrbot_data_path is None:
            return ""
        try:
            config_path = Path(get_astrbot_data_path()) / "cmd_config.json"
            if not config_path.exists():
                return ""
            data = json.loads(config_path.read_text(encoding="utf-8"))
            for platform in data.get("platform", []):
                if not platform.get("enable", True):
                    continue
                if str(platform.get("type", "")) == self.platform_type:
                    return str(platform.get("id", "")).strip()
        except Exception:
            logger.exception("Failed to detect platform id from cmd_config.json")
        return ""

    def _load_state(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return {"sent": {}}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("sent", {})
                return data
        except Exception:
            logger.exception("Failed to load birthday reminder state")
        return {"sent": {}}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_file.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @filter.command("birthday_check")
    async def birthday_check(self, event: AstrMessageEvent):
        entries, errors = self._load_birthdays()
        today = self._now().date()
        today_entries = self._entries_for_day(entries, today)
        event_group_id = str(event.get_group_id() or "").strip()
        if event_group_id:
            today_entries = [
                entry
                for entry in today_entries
                if entry.group_id == event_group_id
                or entry.group_id.endswith(f":{event_group_id}")
            ]
        if today_entries:
            yield event.plain_result(
                await self._build_birthday_message(
                    today_entries,
                    today,
                    event.unified_msg_origin,
                )
            )
        else:
            yield event.plain_result(f"今天（{today:%m-%d}）没有生日。")

        if errors:
            yield event.plain_result("生日文件有格式问题：\n" + "\n".join(errors[:5]))

    @filter.command("birthday_reload")
    async def birthday_reload(self, event: AstrMessageEvent):
        entries, errors = self._load_birthdays()
        message = f"已读取 {len(entries)} 条生日记录。"
        if errors:
            message += "\n格式问题：\n" + "\n".join(errors[:10])
        yield event.plain_result(message)

    @filter.command("birthday_next")
    async def birthday_next(self, event: AstrMessageEvent):
        entries, errors = self._load_birthdays()
        event_group_id = str(event.get_group_id() or "").strip()
        if event_group_id:
            entries = [
                entry
                for entry in entries
                if entry.group_id == event_group_id
                or entry.group_id.endswith(f":{event_group_id}")
            ]
        today = self._now().date()
        upcoming = upcoming_birthdays(entries, today, limit=5)
        if not upcoming:
            yield event.plain_result("还没有可用的生日记录，请编辑 birthdays.txt。")
            return

        lines = ["最近的生日："]
        for target_day, entry in upcoming:
            lines.append(f"{target_day:%m-%d}：{entry.detail_on(target_day)}")
        if errors:
            lines.append(
                f"另有 {len(errors)} 行格式有问题，可用 /birthday_reload 查看。"
            )
        yield event.plain_result("\n".join(lines))

    async def terminate(self) -> None:
        self._stopped.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Birthday reminder plugin terminated")


DATE_PATTERNS = [
    re.compile(
        r"(?P<year>\d{4})\s*(?:-|/|\.|年)\s*(?P<month>\d{1,2})\s*(?:-|/|\.|月)\s*(?P<day>\d{1,2})\s*日?"
    ),
    re.compile(
        r"(?<!\d)(?P<month>\d{1,2})\s*(?:-|/|\.|月)\s*(?P<day>\d{1,2})\s*日?(?!\d)"
    ),
]


def parse_birthday_line(
    line: str, line_no: int
) -> tuple[BirthdayEntry | None, str | None]:
    text = line.strip()
    if not text or text.startswith("#"):
        return None, None

    for pattern in DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue

        year_text = match.groupdict().get("year")
        year = int(year_text) if year_text else None
        month = int(match.group("month"))
        day = int(match.group("day"))
        validation_year = year or 2000
        try:
            date(validation_year, month, day)
        except ValueError:
            return None, f"第 {line_no} 行生日日期无效"

        name = text[: match.start()].strip(" \t,，|｜:：;；-—")
        group_id = _extract_group_id(text[match.end() :])
        if not name:
            return None, f"第 {line_no} 行缺少姓名"
        if not group_id:
            return None, f"第 {line_no} 行缺少群号"
        return BirthdayEntry(
            name=name,
            month=month,
            day=day,
            year=year,
            group_id=group_id,
            line_no=line_no,
        ), None

    return None, f"第 {line_no} 行未找到生日日期"


def upcoming_birthdays(
    entries: list[BirthdayEntry],
    today: date,
    limit: int = 5,
) -> list[tuple[date, BirthdayEntry]]:
    upcoming: list[tuple[date, BirthdayEntry]] = []
    for entry in entries:
        target = _next_birthday_date(entry, today)
        if target:
            upcoming.append((target, entry))
    upcoming.sort(key=lambda item: (item[0], item[1].name))
    return upcoming[:limit]


def _extract_group_id(text: str) -> str:
    text = text.strip(" \t,，|｜:：;；-—")
    if not text:
        return ""
    parts = text.split()
    if not parts:
        return ""
    return parts[-1].strip(" \t,，|｜:：;；")


def _extract_persona_prompt(persona: Any) -> str:
    if persona is None:
        return ""
    if isinstance(persona, dict):
        return str(persona.get("prompt") or persona.get("system_prompt") or "").strip()
    return str(
        getattr(persona, "prompt", "") or getattr(persona, "system_prompt", "") or ""
    ).strip()


def _limit_text(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n..."


def _as_positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _looks_mojibake(text: str) -> bool:
    if not text:
        return False
    marker_count = sum(text.count(marker) for marker in MOJIBAKE_MARKERS)
    replacement_count = text.count("?")
    return marker_count >= 3 or replacement_count >= max(12, len(text) // 5)


def _is_unusable_message(message: str) -> bool:
    if not message:
        return True
    if _looks_mojibake(message):
        return True
    normalized = message.lower()
    confusing_phrases = ("看不懂", "乱码", "无法理解", "不明白", "按到键盘")
    return any(phrase in normalized for phrase in confusing_phrases)


def _next_birthday_date(entry: BirthdayEntry, today: date) -> date | None:
    for year in range(today.year, today.year + 5):
        try:
            target = date(year, entry.month, entry.day)
        except ValueError:
            continue
        if target >= today:
            return target
    return None

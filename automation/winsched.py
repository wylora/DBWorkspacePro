"""Задание Планировщика Windows: позволяет выполнять задачу по расписанию, даже когда программа закрыта.

Создаётся XML-описание (не зависит от языка системы) и команда schtasks. Задание запускается от имени текущего
пользователя и только когда он вошёл в систему — так ему доступны сохранённые пароли (Credential Manager)
и не нужно хранить пароль Windows в планировщике.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from .schedule import Schedule, parse_dt
from .store import ScheduledTask

NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")


def supported(s: Schedule) -> tuple[bool, str]:
    if s.kind == "on_connect":
        return False, "Запуск «после подключения» возможен только в работающей программе."
    if s.kind == "monthly" and int(s.day) in (29, 30):
        return False, ("Планировщик Windows не умеет «29/30-е или последний день, если дня нет». Выберите день 1–28 "
                       "или 31 (последний день месяца).")
    return True, ""


def launch_command(task_id: str, main_py: Path | None = None) -> tuple[str, str, str]:
    """(программа, аргументы, рабочая папка)."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        return str(exe), f"--run-task {task_id}", str(exe.parent)
    main_py = main_py or Path(__file__).resolve().parents[1] / "main.py"
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    return str(pyw if pyw.exists() else py), f'"{main_py}" --run-task {task_id}', str(main_py.parent)


def _trigger(s: Schedule, now: dt.datetime) -> str:
    if s.kind == "once":
        return f"<TimeTrigger><StartBoundary>{parse_dt(s.at):%Y-%m-%dT%H:%M:%S}</StartBoundary><Enabled>true</Enabled></TimeTrigger>"
    if s.kind == "interval":
        start = parse_dt(s.start) if s.start else now.replace(second=0, microsecond=0)
        return (f"<TimeTrigger><Repetition><Interval>PT{int(s.interval_minutes)}M</Interval></Repetition>"
                f"<StartBoundary>{start:%Y-%m-%dT%H:%M:%S}</StartBoundary><Enabled>true</Enabled></TimeTrigger>")
    hh, mm = s.time.split(":")
    start = f"{now:%Y-%m-%d}T{int(hh):02d}:{int(mm):02d}:00"
    if s.kind == "daily":
        sched = "<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"
    elif s.kind == "weekly":
        days = "".join(f"<{DAYS[d]} />" for d in sorted(set(s.weekdays)))
        sched = f"<ScheduleByWeek><DaysOfWeek>{days}</DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek>"
    else:
        day = "Last" if int(s.day) == 31 else str(int(s.day))
        months = "".join(f"<{m} />" for m in MONTHS)
        sched = f"<ScheduleByMonth><DaysOfMonth><Day>{day}</Day></DaysOfMonth><Months>{months}</Months></ScheduleByMonth>"
    return f"<CalendarTrigger><StartBoundary>{start}</StartBoundary><Enabled>true</Enabled>{sched}</CalendarTrigger>"


def task_xml(t: ScheduledTask, command: str, arguments: str, workdir: str, now: dt.datetime | None = None) -> str:
    ok, why = supported(t.schedule)
    if not ok:
        raise ValueError(why)
    now = now or dt.datetime.now()
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="{NS}">
  <RegistrationInfo>
    <Description>{escape(f"DB Workspace Pro: {t.name}")}</Description>
  </RegistrationInfo>
  <Triggers>{_trigger(t.schedule, now)}</Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>{"true" if t.catch_up else "false"}</StartWhenAvailable>
    <ExecutionTimeLimit>PT12H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(workdir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def task_name(t: ScheduledTask) -> str:
    safe = "".join(ch if ch.isalnum() or ch in " -_()." else "_" for ch in t.name).strip() or t.id
    return f"DBWorkspacePro\\{safe[:80]} [{t.id}]"


def schtasks_create(t: ScheduledTask, xml_path: Path) -> list[str]:
    return ["schtasks", "/Create", "/TN", task_name(t), "/XML", str(xml_path), "/F"]


def schtasks_delete(t: ScheduledTask) -> list[str]:
    return ["schtasks", "/Delete", "/TN", task_name(t), "/F"]


def write_xml(t: ScheduledTask, path: Path) -> Path:
    command, args, workdir = launch_command(t.id)
    Path(path).write_text(task_xml(t, command, args, workdir), encoding="utf-16")
    return Path(path)

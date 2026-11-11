"""Run this script on a Windows PC that should be added to WakeBot."""

import getpass
import json
import platform
import socket
import subprocess
import uuid


def normalize_mac(value: str) -> str:
    return value.strip().replace("-", ":").upper()


def fallback_mac() -> str:
    mac = uuid.getnode()
    return ":".join(f"{(mac >> (8 * index)) & 0xFF:02X}" for index in reversed(range(6)))


def get_local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "CHANGE_ME"
    finally:
        sock.close()


def get_windows_adapters() -> list[dict[str, str]]:
    if platform.system().casefold() != "windows":
        return []

    command = (
        "Get-NetAdapter | Where-Object Status -eq 'Up' | "
        "Select-Object Name, InterfaceDescription, MacAddress | ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        if not result.stdout.strip():
            return []
        values = json.loads(result.stdout)
        if isinstance(values, dict):
            values = [values]
        return [
            {
                "name": str(value.get("Name", "")),
                "description": str(value.get("InterfaceDescription", "")),
                "mac": normalize_mac(str(value.get("MacAddress", ""))),
            }
            for value in values
            if value.get("MacAddress")
        ]
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []


def main() -> None:
    adapters = get_windows_adapters()
    if not adapters:
        adapters = [{"name": "default", "description": "fallback", "mac": fallback_mac()}]

    base = {
        "name": socket.gethostname(),
        "ip": get_local_ip(),
        "ssh_user": getpass.getuser(),
        "os": platform.system(),
        "users": [],
    }

    print("=== Active network adapters ===")
    for index, adapter in enumerate(adapters, start=1):
        print(f"{index}. {adapter['name']}: {adapter['mac']} ({adapter['description']})")

    print("\n=== Ready-to-paste COMPUTERS entries ===")
    print("Choose the Ethernet adapter that has Wake-on-LAN enabled.")
    for adapter in adapters:
        entry = {**base, "mac": adapter["mac"]}
        print(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()

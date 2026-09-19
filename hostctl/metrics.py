import os
import subprocess


def host() -> dict[str, float]:
    load1 = os.getloadavg()[0]
    with open("/proc/meminfo") as f:
        meminfo = {
            k.strip(): float(v.split()[0]) / 1_048_576
            for k, v in (line.split(":", 1) for line in f.read().splitlines())
        }
    arc = subprocess.run(
        ["awk", "/^size/ {print $3}", "/proc/spl/kstat/zfs/arcstats"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    with open("/proc/uptime") as f:
        uptime_s = float(f.read().split()[0])
    return {
        "load1": load1,
        "mem_total_gb": round(meminfo["MemTotal"], 2),
        "mem_used_gb": round(meminfo["MemTotal"] - meminfo["MemAvailable"], 2),
        "arc_gb": round(float(arc) / 1_073_741_824, 2) if arc else 0.0,
        "uptime_s": uptime_s,
    }

from pathlib import Path


def main() -> int:
    path = Path.home() / "AppData" / "Local" / "Temp" / "openclaw" / "openclaw-2026-04-04.log"
    if not path.exists():
        print(f"missing: {path}")
        return 1
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-120:]:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

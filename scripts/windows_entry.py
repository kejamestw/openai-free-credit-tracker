"""PyInstaller entry point that preserves the quota_monitor package context."""

from quota_monitor.app import main


if __name__ == "__main__":
    main()

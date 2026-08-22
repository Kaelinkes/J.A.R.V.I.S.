import subprocess
import psutil
import win32gui
import win32con


# Map friendly / spoken names to the actual Windows process name (no .exe needed).
# Add to this as you find more mismatches.
PROCESS_ALIASES = {
    "task manager": "taskmgr",
    "file explorer": "explorer",
    "explorer": "explorer",
    "calculator": "calculatorapp",
    "notepad": "notepad",
    "settings": "systemsettings",
    "control panel": "control",
    "command prompt": "cmd",
    "powershell": "powershell",
    "microsoft edge": "msedge",
    "edge": "msedge",
    # matches both studio64.exe (64-bit) and studio.exe (32-bit)
    "android studio": "studio",
}


# Windows Settings' AppID is fixed/identical on every Windows install
# (cw5n1h2txyewy is Microsoft's own publisher hash), so we skip the
# fuzzy Get-StartApps search entirely and launch it directly - this
# avoids accidentally matching a third-party app like "HMS Settings".
KNOWN_APP_IDS = {
    "systemsettings": "windows.immersivecontrolpanel_cw5n1h2txyewy!microsoft.windows.immersivecontrolpanel",
}

# Apps where substring matching on process name is dangerous because a
# background helper process shares the same substring. For these, match
# the exact process filename only.
# e.g. "SystemSettingsBroker.exe" is always running and would substring-
# match "systemsettings", but it's NOT the actual Settings window and
# is a protected process that can't be terminated anyway.
EXACT_PROCESS_NAMES = {
    "systemsettings": "systemsettings.exe",
}

# Apps whose process (or a background broker for it) is always running,
# even when no window is visible. is_running() would false-positive on
# these, so open_application skips the "already open" check for them.
ALWAYS_RELAUNCH = {"explorer", "systemsettings"}


def normalize(name):
    """Lowercase and strip spaces so 'task manager' and 'taskmgr' can be compared fairly."""
    return name.strip().lower().replace(" ", "")


class ApplicationController:

    # ---------------------------------------------------------
    # FIND APPLICATION USING WINDOWS START MENU
    # ---------------------------------------------------------

    def find_application(self, app_name):

        app_name = app_name.strip()

        powershell_command = f"""
        Get-StartApps |
        Where-Object {{ $_.Name -like "*{app_name}*" }} |
        Select-Object -First 1 |
        ConvertTo-Json -Compress
        """

        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    powershell_command
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore"
            )

            output = result.stdout.strip()

            if not output:
                return None

            import json

            app = json.loads(output)

            return app

        except Exception as error:

            print(f"Application search error: {error}")

            return None

    # ---------------------------------------------------------
    # RESOLVE A FRIENDLY NAME TO THE PROCESS NAME TO MATCH ON
    # ---------------------------------------------------------

    def resolve_process_target(self, app_name):

        key = app_name.strip().lower()

        if key in PROCESS_ALIASES:
            return PROCESS_ALIASES[key]

        # Fall back to normalized (space-stripped) version of whatever was typed
        return normalize(app_name)

    # ---------------------------------------------------------
    # CHECK IF APPLICATION IS RUNNING
    # ---------------------------------------------------------

    def is_running(self, app_name):

        target = self.resolve_process_target(app_name)
        exact_match = EXACT_PROCESS_NAMES.get(target)

        for process in psutil.process_iter(["name", "exe"]):

            try:
                process_name = process.info["name"]

                if not process_name:
                    continue

                normalized_process = normalize(process_name)

                if exact_match:
                    if normalized_process == exact_match:
                        return True
                elif target in normalized_process:
                    return True

            except (
                psutil.NoSuchProcess,
                psutil.AccessDenied,
                psutil.ZombieProcess
            ):
                continue

        return False

    # ---------------------------------------------------------
    # OPEN APPLICATION
    # ---------------------------------------------------------

    def open_application(self, app_name):

        target = self.resolve_process_target(app_name)

        # File Explorer: its process is always running (it's the shell),
        # so just spawn a fresh window directly - no lookup needed.
        if target == "explorer":
            try:
                subprocess.Popen(["explorer.exe"])
                print("Opening now")
            except Exception as error:
                print(f"Could not open File Explorer: {error}")
            return

        # Settings and anything else in ALWAYS_RELAUNCH: their process/broker
        # lingers in the background even when no window is open, so the
        # is_running() check would false-positive. Skip it for these.
        if target not in ALWAYS_RELAUNCH and self.is_running(app_name):
            print("App is already open")
            return

        print(f"Searching for {app_name}...")

        if target in KNOWN_APP_IDS:
            app_id = KNOWN_APP_IDS[target]
            print(f"Found: {app_name.capitalize()} (known app)")

            try:
                subprocess.Popen(
                    [
                        "explorer.exe",
                        f"shell:AppsFolder\\{app_id}"
                    ]
                )
                print("Opening now")
            except Exception as error:
                print(f"Could not open {app_name}: {error}")

            return

        application = self.find_application(app_name)

        if not application:
            print(f"{app_name.capitalize()} not found")
            return

        app_name_found = application["Name"]
        app_id = application["AppID"]

        print(f"Found: {app_name_found}")

        try:
            subprocess.Popen(
                [
                    "explorer.exe",
                    f"shell:AppsFolder\\{app_id}"
                ]
            )
            print("Opening now")

        except Exception as error:
            print(f"Could not open {app_name}: {error}")

    # ---------------------------------------------------------
    # CLOSE FILE EXPLORER WINDOWS ONLY (NOT THE DESKTOP SHELL)
    # ---------------------------------------------------------

    def close_file_explorer_windows(self):
        """
        File Explorer windows run under explorer.exe, but so does the
        desktop and taskbar. Killing the process kills all of it.
        Instead, find just the Explorer *window* class and ask it to
        close politely via WM_CLOSE - same as clicking the X.
        """

        closed_any = []

        def enum_handler(hwnd, _):

            if not win32gui.IsWindowVisible(hwnd):
                return True

            class_name = win32gui.GetClassName(hwnd)

            # CabinetWClass = modern File Explorer window
            # ExploreWClass = legacy variant, kept for older systems
            if class_name in ("CabinetWClass", "ExploreWClass"):
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                closed_any.append(hwnd)

            return True

        win32gui.EnumWindows(enum_handler, None)

        return len(closed_any)

    # ---------------------------------------------------------
    # CLOSE APPLICATION
    # ---------------------------------------------------------

    def close_application(self, app_name):

        target = self.resolve_process_target(app_name)

        # File Explorer gets special handling: close its windows,
        # never terminate explorer.exe itself.
        if target == "explorer":

            count = self.close_file_explorer_windows()

            if count:
                print(f"Closed {count} File Explorer window(s)")
            else:
                print("No File Explorer windows open")

            return

        exact_match = EXACT_PROCESS_NAMES.get(target)

        found_process = False
        access_denied = False

        for process in psutil.process_iter(["pid", "name"]):

            try:
                process_name = process.info["name"]

                if not process_name:
                    continue

                normalized_process = normalize(process_name)

                is_match = (
                    normalized_process == exact_match
                    if exact_match
                    else target in normalized_process
                )

                if is_match:
                    found_process = True
                    process.terminate()

            except psutil.AccessDenied:
                found_process = True
                access_denied = True
                continue

            except (
                psutil.NoSuchProcess,
                psutil.ZombieProcess
            ):
                continue

        if access_denied:
            print(
                f"{app_name.capitalize()} is running but access was denied. "
                "Run this script as Administrator to close it."
            )
        elif found_process:
            print("Closing app")
        else:
            print("Already closed")


# =============================================================
# COMMAND PROCESSOR
# =============================================================

def process_command(controller, command):

    command = command.strip()

    if not command:
        return

    command_lower = command.lower()

    if command_lower.startswith("open "):
        app_name = command[5:].strip()
        if app_name:
            controller.open_application(app_name)
        return

    if command_lower.startswith("close "):
        app_name = command[6:].strip()
        if app_name:
            controller.close_application(app_name)
        return

    if command_lower in ["exit", "quit"]:
        return "exit"

    print("I don't understand that command.")


# =============================================================
# MAIN
# =============================================================

def main():

    controller = ApplicationController()

    print("=" * 50)
    print("       J.A.R.V.I.S. SYSTEM CONTROLLER")
    print("=" * 50)

    print()
    print("Controller online.")
    print()
    print("Try:")
    print("  open whatsapp")
    print("  close whatsapp")
    print("  open task manager")
    print("  close task manager")
    print("  exit")
    print()

    while True:
        command = input("JARVIS > ")
        result = process_command(controller, command)
        if result == "exit":
            print("J.A.R.V.I.S. shutting down.")
            break


if __name__ == "__main__":
    main()
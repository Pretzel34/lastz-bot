; Last Z Bot — NSIS Installer Script
; Build with: makensis /DAPP_VERSION=0.1.0 installer.nsi
; Supports silent install via /S flag (used by auto-update)

!ifndef APP_VERSION
  !define APP_VERSION "0.1.0"
!endif

!define APP_NAME      "Last Z Bot"
!define APP_EXE       "LastZBot.exe"
!define INSTALL_DIR   "$PROGRAMFILES\LastZBot"
!define REG_KEY       "Software\Microsoft\Windows\CurrentVersion\Uninstall\LastZBot"
!define STARTMENU_DIR "$SMPROGRAMS\Last Z Bot"

Name              "${APP_NAME}"
OutFile           "dist\LastZBot-v${APP_VERSION}-Setup.exe"
InstallDir        "${INSTALL_DIR}"
RequestExecutionLevel admin
SetCompressor     lzma

; ── Pages ────────────────────────────────────────────────────────────────────
; Shown only when NOT running silently (/S suppresses all pages)
Page instfiles

UninstPage uninstConfirm
UninstPage instfiles

; ── Install ──────────────────────────────────────────────────────────────────
Section "Install"
  SetOutPath "$INSTDIR"
  File "dist\${APP_EXE}"

  ; Start Menu shortcut
  CreateDirectory "${STARTMENU_DIR}"
  CreateShortCut  "${STARTMENU_DIR}\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}"

  ; Desktop shortcut
  CreateShortCut  "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}"

  ; Write uninstaller
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  ; Always run as administrator (required for MEmu CLI access)
  WriteRegStr HKLM "SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers" \
    "$INSTDIR\${APP_EXE}" "RUNASADMIN"

  ; Register in Add/Remove Programs
  WriteRegStr   HKLM "${REG_KEY}" "DisplayName"      "${APP_NAME}"
  WriteRegStr   HKLM "${REG_KEY}" "DisplayVersion"   "${APP_VERSION}"
  WriteRegStr   HKLM "${REG_KEY}" "Publisher"        "LastZBot"
  WriteRegStr   HKLM "${REG_KEY}" "InstallLocation"  "$INSTDIR"
  WriteRegStr   HKLM "${REG_KEY}" "UninstallString"  "$INSTDIR\Uninstall.exe"
  WriteRegDWORD HKLM "${REG_KEY}" "NoModify"         1
  WriteRegDWORD HKLM "${REG_KEY}" "NoRepair"         1
SectionEnd

; ── Uninstall ────────────────────────────────────────────────────────────────
Section "Uninstall"
  Delete "$INSTDIR\${APP_EXE}"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir  "$INSTDIR"

  Delete "${STARTMENU_DIR}\${APP_NAME}.lnk"
  RMDir  "${STARTMENU_DIR}"
  Delete "$DESKTOP\${APP_NAME}.lnk"

  DeleteRegKey HKLM "${REG_KEY}"
  DeleteRegValue HKLM "SOFTWARE\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers" \
    "$INSTDIR\${APP_EXE}"

  ; NOTE: %APPDATA%\LastZBot\ (farms.json, logs/) is intentionally left intact
  ;       so user settings survive uninstall/reinstall.
SectionEnd

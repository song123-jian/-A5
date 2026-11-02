Unicode true
RequestExecutionLevel user
ManifestDPIAware true
SetCompressor /SOLID lzma
CRCCheck on

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"
!include "FileFunc.nsh"

!ifndef APP_VERSION
  !error "Pass /DAPP_VERSION=major.minor.patch"
!endif
!ifndef SOURCE_EXE
  !error "Pass /DSOURCE_EXE=absolute application EXE path"
!endif
!ifndef APP_ICON
  !error "Pass /DAPP_ICON=absolute ICO path"
!endif
!ifndef OUTPUT_FILE
  !error "Pass /DOUTPUT_FILE=absolute installer path"
!endif

!define APP_NAME "打印快捷工具"
!define APP_ID "A5TicketPrinter"
!define APP_EXE "打印快捷工具.exe"
!define PRODUCT_KEY "Software\${APP_ID}"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}"

Name "${APP_NAME}"
OutFile "${OUTPUT_FILE}"
InstallDir "$LOCALAPPDATA\Programs\${APP_ID}"
InstallDirRegKey HKCU "${PRODUCT_KEY}" "InstallDir"
BrandingText "${APP_NAME} v${APP_VERSION}"
Icon "${APP_ICON}"
UninstallIcon "${APP_ICON}"
VIProductVersion "${APP_VERSION}.0"
VIAddVersionKey /LANG=2052 "ProductName" "${APP_NAME}"
VIAddVersionKey /LANG=2052 "ProductVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=2052 "FileDescription" "${APP_NAME}安装程序"
VIAddVersionKey /LANG=2052 "FileVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=2052 "LegalCopyright" ""

!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TITLE "安装 ${APP_NAME} v${APP_VERSION}"
!define MUI_WELCOMEPAGE_TEXT "安装程序将为当前用户安装打印快捷工具。$\r$\n$\r$\n您可以选择任意有写入权限的文件夹。升级前，请从托盘右键菜单选择“退出程序”。$\r$\n$\r$\n卸载只移除程序和快捷方式，保留用户配置、日志和原始票据。"
!define MUI_DIRECTORYPAGE_TEXT_TOP "请选择安装文件夹，支持其他磁盘和中文路径。配置和缓存仍保存在当前用户的本地应用数据目录。"
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "运行${APP_NAME}"
!define MUI_FINISHPAGE_RUN_NOTCHECKED
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

Section "主程序和开始菜单快捷方式（必选）" MainSection
  SectionIn RO
  SetShellVarContext current
  SetRegView 64
  ClearErrors
  SetOutPath "$INSTDIR"
  IfErrors install_failed
  SetOverwrite on
  ClearErrors
  File /oname=${APP_EXE} "${SOURCE_EXE}"
  IfErrors install_failed
  ClearErrors
  File /oname=使用说明.txt "${__FILEDIR__}\使用说明.txt"
  IfErrors install_failed
  ClearErrors
  WriteUninstaller "$INSTDIR\卸载.exe"
  IfErrors install_failed
  ClearErrors
  WriteRegStr HKCU "${PRODUCT_KEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\${APP_EXE}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '$\"$INSTDIR\卸载.exe$\"'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "QuietUninstallString" '$\"$INSTDIR\卸载.exe$\" /S'
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
  IfErrors install_failed
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "EstimatedSize" $0
  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\使用说明.lnk" "$INSTDIR\使用说明.txt"
  CreateShortCut "$SMPROGRAMS\${APP_NAME}\卸载.lnk" "$INSTDIR\卸载.exe"
  IfErrors install_failed
  Goto install_done
install_failed:
  SetErrorLevel 2
  MessageBox MB_ICONSTOP "安装未完成。请确认安装目录可写，并从托盘退出正在运行的程序后重试。" /SD IDOK
  Abort
install_done:
SectionEnd

Section "桌面快捷方式" DesktopSection
  SetShellVarContext current
  CreateShortCut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}"
SectionEnd

Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "本程序需要 64 位 Windows。"
    Abort
  ${EndIf}
  SetShellVarContext current
  SetRegView 64
FunctionEnd

Function .onVerifyInstDir
  ${GetRoot} "$INSTDIR" $0
  ${If} "$INSTDIR" == "$0"
  ${OrIf} "$INSTDIR" == "$0\"
    Abort
  ${EndIf}
FunctionEnd

Section "Uninstall"
  SetShellVarContext current
  SetRegView 64
  Delete "$INSTDIR\${APP_EXE}"
  ${If} ${FileExists} "$INSTDIR\${APP_EXE}"
    MessageBox MB_ICONSTOP "程序仍在运行或文件被占用。请从托盘右键菜单选择“退出程序”，然后重新卸载。"
    Abort
  ${EndIf}
  Delete "$INSTDIR\使用说明.txt"
  Delete "$INSTDIR\卸载.exe"
  Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\使用说明.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\卸载.lnk"
  RMDir "$SMPROGRAMS\${APP_NAME}"
  Delete "$DESKTOP\${APP_NAME}.lnk"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  DeleteRegKey HKCU "${PRODUCT_KEY}"
  ; Never recursively remove the install folder or touch LOCALAPPDATA user data.
  RMDir "$INSTDIR"
SectionEnd

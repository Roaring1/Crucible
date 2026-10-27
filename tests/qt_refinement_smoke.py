"""Real PyQt interaction checks, isolated from legacy tests' optional-module stubs."""
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from PyQt6.QtWidgets import QApplication, QMessageBox, QInputDialog
from PyQt6.QtTest import QTest
from PyQt6.QtCore import Qt, QPoint, QPointF, QMimeData, QUrl
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from crucible.data.instance_manager import InstanceManager
from crucible.data.instance_model import ServerInstance
from crucible.ui.main_window import MainWindow
from crucible.ui.tabs.console_tab import ConsoleTab
from crucible.ui.tabs.players_tab import PlayersTab
from crucible.ui.tabs.mods_tab import ModsTab
from crucible.ui.tabs.system_tab import SystemTab
from crucible.ui.add_mod_dialog import AddModDialog
from crucible.ui.add_dialog import AddInstanceDialog
from crucible.mods.mod_manager import ModEntry
from crucible.ui import theme

app=QApplication([]);app.setStyleSheet(theme.STYLESHEET)
root=Path(tempfile.mkdtemp(prefix='crucible-qt-test-'))
inst=ServerInstance(str(root/'server'),'Test server',minecraft_version='1.21.1',loader='fabric',loader_version='0.16.10')
box=ConsoleTab();box._instance=inst;box.resize(760,500);box.show();box._cmd_input.setFocus();QTest.qWait(30)
QTest.keyClicks(box._cmd_input,'gameru')
assert box._suggestions.isVisible() and box._suggestions.item(0).text()=='gamerule'
QTest.keyClick(box._cmd_input,Qt.Key.Key_Tab)
assert box._cmd_input.text()=='gamerule'
QTest.keyClicks(box._cmd_input,' keepInv')
assert box._suggestions.item(0).text()=='keepInventory'
QTest.keyClick(box._cmd_input,Qt.Key.Key_Tab)
assert box._cmd_input.text()=='gamerule keepInventory'
QTest.keyClicks(box._cmd_input,' t')
QTest.keyClick(box._cmd_input,Qt.Key.Key_Tab)
assert box._cmd_input.text()=='gamerule keepInventory true'
box._cmd_input.setText('/gameru keepInventory false');box._cmd_input.setCursorPosition(7);box._reset_completion();box._show_suggestions();box._handle_tab()
assert box._cmd_input.text()=='/gamerule keepInventory false'
box._cmd_input.clear();QTest.keyClicks(box._cmd_input,'a')
assert box._suggestions.count()>=2
QTest.keyClick(box._cmd_input,Qt.Key.Key_Down);QTest.keyClick(box._cmd_input,Qt.Key.Key_Tab)
assert box._cmd_input.text()=='attribute'
QTest.keyClick(box._cmd_input,Qt.Key.Key_Escape);assert box._suggestions.isHidden()
box._suggestions.hide();box._history.extend(['list','say hello']);box._hist_idx=-1
QTest.keyClick(box._cmd_input,Qt.Key.Key_Up);assert box._cmd_input.text()=='say hello'
box.close()

players=PlayersTab();players.resize(720,600);players.show();QTest.qWait(20)
assert players._online_list.minimumHeight()<players._online_list.maximumHeight()
players._players_splitter.setSizes([300,200]);app.processEvents();assert players._online_list.height()>200
players.close()

# Sorting may move metadata results: data must follow the mod, not its original row number.
mods=ModsTab();first=ModEntry('z.jar',root/'z.jar',True,10);second=ModEntry('a.jar',root/'a.jar',True,10)
mods._instance=inst;mods._populate_table([first,second]);mods._table.sortItems(mods._COL_NAME)
first.name='Zeta';first.version='2';first.side='client'
mods._on_inspect_result(mods._inspect_generation,0,first)
for row in range(mods._table.rowCount()):
 item=mods._table.item(row,mods._COL_NAME)
 if item.data(Qt.ItemDataRole.UserRole) is first:
  assert item.text()=='Zeta' and 'Client only' in mods._table.item(row,mods._COL_SIDE).text()
mods.close()

# Consecutive network workflows must be allowed after native thread retirement.
seen=[]
mods=ModsTab()
def second_done(value):seen.append(value)
def first_done(value):
 seen.append(value);mods._run_net(lambda:'second',second_done)
mods._run_net(lambda:'first',first_done)
deadline=time.monotonic()+4
while (len(seen)<2 or mods.has_active_operation()) and time.monotonic()<deadline:QTest.qWait(10)
assert seen==['first','second'],seen
mods.close()

with patch.object(AddModDialog,'_do_search',lambda *args,**kw:None):
 browser=AddModDialog(inst);assert browser._provider.count()==2
 browser._provider.setCurrentText('CurseForge');assert browser._provider.currentText()=='CurseForge';browser.reject()

m=InstanceManager(root/'registry');m.load();(root/'server').mkdir()
inst=m.add_instance(str(root/'server'),'Before',minecraft_version='1.21.1',loader='fabric')
add=AddInstanceDialog(m);add.resize(700,560);add.show();QTest.qWait(20)
for field in (add._path_edit,add._name_edit,add._ver_edit,add._session_edit):assert field.height()>=32
add.reject()
with patch.object(MainWindow,'_run_crash_recovery',lambda self:None):
 window=MainWindow(m);window.resize(900,600);window.show();QTest.qWait(80)
 with patch.object(QInputDialog,'getText',return_value=('Renamed',True)):
  window._on_rename_requested(inst)
 assert m.instances[0].name=='Renamed'
 assert window._panel._name_label.text()=='Renamed'
 assert m.instances[0].path==inst.path and m.instances[0].tmux_session==inst.tmux_session
 assert json.loads(m.registry_file.read_text())['instances'][0]['name']=='Renamed'
 # External server folder drops are accepted even over a child tab view.
 dropdir=root/'drop';dropdir.mkdir();(dropdir/'server.properties').touch()
 mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(dropdir))])
 event=QDragEnterEvent(QPoint(3,3),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
 assert window.eventFilter(window._panel._console._view,event) and event.isAccepted()
 with patch.object(QInputDialog,'getText',return_value=('Dropped name',True)):
  window._import_dropped_path(dropdir)
 assert m.instances[-1].name=='Dropped name'
 # Compact header keeps its action controls inside the panel.
 for btn in (window._panel._btn_start,window._panel._btn_stop,window._panel._btn_restart):
  assert btn.geometry().right()<=btn.parentWidget().width()
 window.close();QTest.qWait(250)
# Destroy Qt objects while QApplication and SIP are still fully initialized.
# Python 3.14 interpreter finalization otherwise destroys global wrappers in an
# undefined order, occasionally crashing after every assertion already passed.
from PyQt6.QtCore import QCoreApplication, QEvent
for widget in app.topLevelWidgets():
 widget.close()
 widget.deleteLater()
QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
app.processEvents()
print('Qt workflow smoke passed: live suggestions, caret completion, keyboard, resizing, sorting, workers, provider, rename, drop')

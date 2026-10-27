"""Real PyQt interaction checks, isolated from legacy tests' optional-module stubs."""
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
from PyQt6.QtWidgets import QApplication, QMessageBox, QInputDialog, QMenu
from PyQt6.QtTest import QTest
from PyQt6.QtCore import Qt, QPoint, QPointF, QMimeData, QUrl
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from crucible.data.instance_manager import InstanceManager
from crucible.data.instance_model import ServerInstance
from crucible.ui.main_window import MainWindow
from crucible.ui.instance_panel import InstancePanel
from crucible.ui.tabs.console_tab import ConsoleTab
from crucible.ui.tabs.players_tab import PlayersTab
from crucible.ui.tabs.mods_tab import ModsTab
from crucible.ui.tabs.system_tab import SystemTab
from crucible.ui.add_mod_dialog import AddModDialog
from crucible.ui.add_dialog import AddInstanceDialog
from crucible.ui.new_server_dialog import NewServerDialog
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
empty_manager=InstanceManager(root/'empty-registry');empty_manager.load()
empty_panel=InstancePanel(empty_manager);empty_panel.resize(900,560);empty_panel.show();QTest.qWait(20)
assert empty_panel._empty_title.text()=='No servers yet'
assert empty_panel._empty_state.isVisible() and empty_panel._tabs.isHidden() and empty_panel._header.isHidden()
assert not empty_panel._btn_start.isVisible() and not empty_panel._btn_ip.isVisible()
empty_panel.close();empty_panel.deleteLater();QTest.qWait(10)
inst=m.add_instance(str(root/'server'),'Before',minecraft_version='1.21.1',loader='fabric')
add=AddInstanceDialog(m);add.resize(700,560);add.show();QTest.qWait(20)
assert not add._advanced_body.isVisible(), 'advanced registration fields should be hidden by default'
add._advanced_toggle.click();app.processEvents();assert add._advanced_body.isVisible()
add._advanced_toggle.click();app.processEvents();assert not add._advanced_body.isVisible()
for field in (add._path_edit,add._name_edit,add._ver_edit,add._session_edit):assert field.height()>=32
add.reject()

# Advanced version settings stay out of the way, and creating a server must
# refuse a non-empty destination rather than overwriting files.
with patch.object(NewServerDialog,'_reload_versions',lambda self:None):
 fresh=NewServerDialog(m)
fresh.resize(720,620);fresh.show();QTest.qWait(20)
assert fresh._name_edit.text() == 'My Server'
assert fresh._path_edit.text(), 'default server name should produce a real destination path'
assert not fresh._advanced_body.isVisible()
fresh._advanced_toggle.click();app.processEvents();assert fresh._advanced_body.isVisible()
ix=fresh._loader_combo.findText('NeoForge')
if ix >= 0:
 fresh._loader_combo.setCurrentIndex(ix);app.processEvents()
 assert fresh._lver_row_label.isVisible()
destinations=root/'destinations';destinations.mkdir()
(destinations/'taken').mkdir();(destinations/'taken-2').mkdir()
assert fresh._unique_folder_path(destinations,'taken') == destinations/'taken-3'
occupied=root/'occupied';occupied.mkdir();sentinel=occupied/'keep.txt';sentinel.write_text('keep')
fresh._ver_combo.setEditText('1.21.1');fresh._name_edit.setText('occupied');fresh._path_edit.setText(str(occupied))
with patch('crucible.ui.new_server_dialog.QMessageBox.warning') as warning:
 fresh._on_create()
assert warning.called and not fresh._busy
assert sentinel.read_text()=='keep', 'fresh server creation must preserve existing files'
fresh.close();QTest.qWait(20)

with patch.object(MainWindow,'_run_crash_recovery',lambda self:None):
 window=MainWindow(m);window.resize(900,600);window.show();QTest.qWait(80)
 # The add-server action is a small menu, not a chooser dialog stacked in front
 # of the actual workflow. Keep all three labels explicit and verify each route.
 for choice, module_path in (
  (0, 'crucible.ui.new_server_dialog.NewServerDialog'),
  (1, 'crucible.ui.modpack_dialog.ModpackDialog'),
  (2, None),
 ):
  chosen=[choice];captured=[]
  def fake_menu_exec(menu,*args):
   captured.append([action.text() for action in menu.actions()])
   return menu.actions()[chosen[0]]
  labels=['Create a server…','Install a modpack…','Import existing files…']
  if choice == 0:
   fake=MagicMock();fake.result_instance=None;fake.exec.return_value=0
   with patch(module_path,return_value=fake) as constructor, patch.object(QMenu,'exec',new=fake_menu_exec):
    window._on_add_requested()
   assert constructor.called
  elif choice == 1:
   fake=MagicMock();fake.result_instance=None;fake.exec.return_value=0
   with patch(module_path,return_value=fake) as constructor, patch.object(QMenu,'exec',new=fake_menu_exec):
    window._on_add_requested()
   assert constructor.called
  else:
   with patch.object(AddInstanceDialog,'exec',return_value=0), patch.object(QMenu,'exec',new=fake_menu_exec):
    window._on_add_requested()
  assert captured == [labels], captured

 # Previewing an imported pack must not write a registry entry until Add server.
 target=root/'not-yet-registered-pack'
 fake_info=SimpleNamespace(
  name='Example pack',version_label='1.2',source_type='modrinth',
  minecraft_version='1.21.1',loader='fabric',loader_version='0.16.10',warnings=[]
 )
 preview=AddInstanceDialog(m)
 preview._import_pending=(str(root/'example.mrpack'),str(target),False)
 preview._complete_prism_import(fake_info,'')
 assert all(Path(item.path) != target for item in m.instances)
 assert not preview._import_folder_btn.isEnabled()
 assert not preview._import_archive_btn.isEnabled()
 preview._finish_prism_import(str(root/'second-pack.mrpack'))
 assert "already ready" in preview._warn_label.text()
 assert preview._pending_import_registration is not None
 preview.reject()
 assert all(Path(item.path) != target for item in m.instances)
 preview.deleteLater();QTest.qWait(10)

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
 assert m.instances[-1].name==dropdir.name
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

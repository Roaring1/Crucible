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
from crucible.ui.add_mod_dialog import AddModDialog, _SearchWorker
from crucible.ui.add_dialog import AddInstanceDialog
from crucible.ui.new_server_dialog import NewServerDialog
from crucible.ui.modpack_dialog import ModpackDialog, _PackInstallWorker
import threading
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

# Closing before the deferred initial search runs must not launch a worker
# against a deleted dialog. Stub the worker so this lifecycle regression never
# accesses the live network or the user's configured CurseForge credential.
search_started=[]
def unexpected_search(worker):
 search_started.append(worker._provider)
 worker.done.emit(worker._offset, [])
with patch.object(_SearchWorker,'run',unexpected_search):
 with patch.object(AddModDialog,'_do_search',lambda *args,**kw:None):
  browser=AddModDialog(inst);assert browser._provider.count()==2
  browser._provider.setCurrentText('CurseForge');assert browser._provider.currentText()=='CurseForge';browser.reject()
 QTest.qWait(40)
assert search_started==[], f'closed Add Mod dialog unexpectedly launched a search: {search_started}'

# The selected provider must route search to its own adapter, not silently
# fall back to Modrinth. These calls use fixtures and never access the network.
from crucible.mods import curseforge, modrinth
with patch.object(curseforge, 'search', return_value=[]) as cf_search, \
     patch.object(modrinth, 'search', return_value=[]) as mr_search:
 worker=_SearchWorker('example','forge','1.20.1',provider='CurseForge')
 worker.run()
 cf_search.assert_called_once_with('example',loader='forge',mc_version='1.20.1',limit=30,offset=0)
 assert not mr_search.called
with patch.object(curseforge, 'search', return_value=[]) as cf_search, \
     patch.object(modrinth, 'search', return_value=[]) as mr_search:
 worker=_SearchWorker('example','forge','1.20.1',provider='Modrinth')
 worker.run()
 mr_search.assert_called_once_with('example',loader='forge',mc_version='1.20.1',limit=30,offset=0)
 assert not cf_search.called

# An identified CurseForge JAR opens its actual project page, resolving the
# official slug asynchronously instead of opening a guessed Modrinth search.
(root/'server').mkdir(exist_ok=True)
(root/'server'/'.crucible').mkdir(exist_ok=True)
(root/'server'/'.crucible'/'added-mods.json').write_text(json.dumps({
 'identified.jar': {'project_id':'cf:123','version_id':'456','identity':'exact-curseforge-fingerprint'}
}))
mods=ModsTab();mods._instance=inst
identified=ModEntry('identified.jar',root/'server'/'mods'/'identified.jar',True,10)
with patch.object(ModsTab,'_run_net',lambda self,fn,on_done,busy:on_done(fn())), \
     patch.object(curseforge,'project_url',return_value='https://www.curseforge.com/minecraft/mc-mods/example-mod') as project_url, \
     patch('PyQt6.QtGui.QDesktopServices.openUrl') as open_url:
 mods._open_mod_page(identified)
 project_url.assert_called_once_with('cf:123')
 assert open_url.call_count==1 and open_url.call_args.args[0].toString()=='https://www.curseforge.com/minecraft/mc-mods/example-mod'
mods.close()

m=InstanceManager(root/'registry');m.load();(root/'server').mkdir(exist_ok=True)
empty_manager=InstanceManager(root/'empty-registry');empty_manager.load()
empty_panel=InstancePanel(empty_manager);empty_panel.resize(900,560);empty_panel.show();QTest.qWait(20)
assert empty_panel._empty_title.text()=='No servers yet'
assert empty_panel._empty_state.isVisible() and empty_panel._tabs.isHidden() and empty_panel._header.isHidden()
assert not empty_panel._btn_start.isVisible() and not empty_panel._btn_ip.isVisible()
empty_panel.close();empty_panel.deleteLater();QTest.qWait(10)
inst=m.add_instance(str(root/'server'),'Before',minecraft_version='1.21.1',loader='fabric')
# An old registry entry can outlive its deleted folder; import target naming
# must avoid that reserved path as well as existing folders on disk.
registered_manager=InstanceManager(root/'registered-import-registry');registered_manager.load()
registered_parent=root/'registered-import-parent';registered_parent.mkdir()
registered_manager.add_instance(str(registered_parent/'Pack'),'Pack')
registered_dialog=AddInstanceDialog(registered_manager)
registered_dialog._destination_parent=registered_parent
with patch('crucible.ui.add_dialog.QFileDialog.getExistingDirectory') as destination_picker:
 assert Path(registered_dialog._choose_prism_target('Pack')) == registered_parent/'Pack-2'
 assert not destination_picker.called, 'the common import path should not force a second folder-picker window'
custom_parent=root/'custom-import-parent';custom_parent.mkdir()
with patch('crucible.ui.add_dialog.QFileDialog.getExistingDirectory',return_value=str(custom_parent)) as destination_picker:
 registered_dialog._browse_destination_parent()
assert destination_picker.called and registered_dialog._destination_parent==custom_parent
assert registered_dialog._destination_label.text()==str(custom_parent)
registered_dialog.reject();registered_dialog.deleteLater();QTest.qWait(10)
add=AddInstanceDialog(m);add.resize(700,560);add.show();QTest.qWait(20)
assert not add._advanced_body.isVisible(), 'advanced registration fields should be hidden by default'
assert not add._add_button.isEnabled(), 'Add server must not be clickable before an import or folder is selected'
add._advanced_toggle.click();app.processEvents();assert add._advanced_body.isVisible()
add._path_edit.setText(str(root/'existing-server'));app.processEvents()
assert add._add_button.isEnabled(), 'choosing a folder should enable Add server'
add._path_edit.clear();app.processEvents()
assert not add._add_button.isEnabled(), 'clearing the folder should disable Add server again'
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
# A leaf symlink to an empty directory is not a safe new-server destination.
real_empty=root/'real-empty-server-target';real_empty.mkdir()
leaf_link=root/'linked-server-target';leaf_link.symlink_to(real_empty,target_is_directory=True)
fresh._path_edit.setText(str(leaf_link))
with patch('crucible.ui.new_server_dialog.QMessageBox.warning') as warning:
 fresh._on_create()
assert warning.called and warning.call_args.args[1]=='Symbolic-link destination'
assert list(real_empty.iterdir())==[], 'fresh server creation must not write through a symlink'
fresh.close();QTest.qWait(20)

# Modrinth server installation uses a folder picker, never a Save File picker;
# its unique target accounts for both files on disk and registry paths.
modpack_parent=root/'modpack-destinations';modpack_parent.mkdir()
(modpack_parent/'Test-Pack').mkdir()
registered_target=modpack_parent/'Test-Pack-2'
registered_only=SimpleNamespace(path=str(registered_target))
with patch.object(ModpackDialog,'_do_search',lambda self,initial=False:None):
 pack_ui=ModpackDialog(m)
assert pack_ui._unique_install_target(
 modpack_parent,'Test Pack',m.instances+[registered_only]
) == modpack_parent/'Test-Pack-3'
m.add_instance(str(registered_target),'Registered target')
hit=SimpleNamespace(
 title='Test Pack',project_id='test-pack-id',downloads=10,author='author',
 icon_url='',page_url=lambda:'https://example.invalid'
)
pack_ui._all_hits=[hit];pack_ui._results.addItem('Test Pack');pack_ui._results.setCurrentRow(0)
pack_ui._eula.setChecked(False)
with patch('crucible.ui.modpack_dialog.QFileDialog.getExistingDirectory') as folder_picker:
 pack_ui._install()
assert not folder_picker.called
assert 'accept the minecraft eula' in pack_ui._status.text().lower()
with patch('crucible.ui.modpack_dialog.QFileDialog.getExistingDirectory',return_value=str(modpack_parent)) as folder_picker:
 chosen_target=pack_ui._choose_install_target(hit)
assert chosen_target == str(modpack_parent/'Test-Pack-3')
assert folder_picker.called

# An install target that appears after the picker is rechecked in the worker;
# the worker must not call into the installer or touch the existing files.
race_target=root/'raced-modpack';race_target.mkdir()
race_marker=race_target/'keep.txt';race_marker.write_text('keep this file')
worker_logs=[];worker_results=[]
worker=_PackInstallWorker('test-pack-id',str(race_target),True,threading.Event())
worker.log.connect(worker_logs.append);worker.finished.connect(worker_results.append)
with patch('crucible.ui.modpack_dialog.modpack_auto.install_modpack_from_modrinth') as install_call:
 worker.run()
assert not install_call.called and worker_results==[None]
assert any('Refusing to install into an existing folder' in line for line in worker_logs)
assert race_marker.read_text()=='keep this file'

fresh_worker_target=root/'fresh-worker-target'
fresh_worker= _PackInstallWorker('test-pack-id',str(fresh_worker_target),True,threading.Event())
fresh_worker_results=[];fresh_worker.finished.connect(fresh_worker_results.append)
fake_install_result=SimpleNamespace(ok=True)
with patch('crucible.ui.modpack_dialog.modpack_auto.install_modpack_from_modrinth',return_value=fake_install_result) as install_call:
 fresh_worker.run()
assert install_call.call_args.kwargs['target_must_not_exist'] is True
assert fresh_worker_results==[fake_install_result]

# Completion is communicated in the existing status/log UI, not a second popup.
success_target=root/'installed-modpack';success_target.mkdir()
pack_ui._pending=(hit,str(success_target))
ok_result=SimpleNamespace(
 path=str(success_target),ok=True,cancelled=False,failed_reason='',
 minecraft_version='1.21.1',loader='fabric',loader_version='0.16.10',
 summary=lambda:'Installed test pack'
)
pack_ui._on_installed(ok_result)
assert pack_ui.result_instance is not None and pack_ui._accept_requested
assert 'added it to your server list' in pack_ui._status.text()

# If a path is already registered, return that row rather than appending a duplicate.
pack_ui._pending=(hit,inst.path)
duplicate_result=SimpleNamespace(
 path=inst.path,ok=True,cancelled=False,failed_reason='',
 minecraft_version='1.21.1',loader='fabric',loader_version='0.16.10',
 summary=lambda:'Existing server'
)
pack_ui._accept_requested=False
pack_ui._on_installed(duplicate_result)
assert pack_ui.result_instance.id==inst.id and pack_ui._accept_requested
pack_ui.close();pack_ui.deleteLater();QTest.qWait(20)

with patch.object(MainWindow,'_run_crash_recovery',lambda self:None):
 window=MainWindow(m);window.resize(900,600);window.show();QTest.qWait(80)
 # Keep five primary pages visible and move infrequent references under More.
 panel=window._panel;tabs=panel._tabs;bar=tabs.tabBar()
 visible=[tabs.tabText(i) for i in range(tabs.count()) if bar.isTabVisible(i)]
 assert len(visible)==5, visible
 assert any('Setup' in label for label in visible)
 assert any('Console' in label for label in visible)
 assert any('Mods' in label for label in visible)
 assert any('World' in label for label in visible)
 assert any('Backups' in label for label in visible)
 assert all(not bar.isTabVisible(i) for i in (4,6,7,8))
 panel._more_tab_actions[4].trigger();app.processEvents()
 assert tabs.currentWidget() is panel._config
 assert panel._more_tabs_button.text().startswith('More: Config')
 assert panel._more_tab_actions[4].isChecked()
 tabs.setCurrentWidget(panel._setup);app.processEvents()
 assert panel._more_tabs_button.text()=='More ▾'
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

 # Reinstall/duplicate results should select an existing sidebar row, not add
 # a duplicate row or claim the server was newly added.
 previous_count=window._sidebar._list.count()
 existing_dialog=MagicMock();existing_dialog.result_instance=inst;existing_dialog.exec.return_value=1
 def choose_existing_pack(menu,*args):return menu.actions()[1]
 with patch('crucible.ui.modpack_dialog.ModpackDialog',return_value=existing_dialog), patch.object(QMenu,'exec',new=choose_existing_pack), patch.object(window._tmux,'get_status',return_value='stopped'):
  window._on_add_requested()
 assert window._sidebar._list.count()==previous_count
 assert window._sidebar.selected_instance().id==inst.id
 assert 'selected existing server' in window.statusBar().currentMessage().lower()

 # Previewing an imported pack must not write a registry entry until Add server.
 target=root/'not-yet-registered-pack'
 fake_info=SimpleNamespace(
  name='Example pack',version_label='1.2',source_type='modrinth',
  minecraft_version='1.21.1',loader='fabric',loader_version='0.16.10',warnings=[]
 )
 failed_preview=AddInstanceDialog(m)
 failed_target=root/'partial-import'
 failed_preview._import_pending=(str(root/'broken.mrpack'),str(failed_target))
 with patch('crucible.ui.add_dialog.QMessageBox.critical') as critical:
  failed_preview._complete_prism_import(None,'source drive disconnected')
 assert not critical.called, 'import failures should be shown inline, not in another popup'
 assert 'Import failed' in failed_preview._warn_label.text()
 assert str(failed_target) in failed_preview._warn_label.text()
 failed_preview.reject();failed_preview.deleteLater();QTest.qWait(10)

 preview=AddInstanceDialog(m)
 preview._import_pending=(str(root/'example.mrpack'),str(target))
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

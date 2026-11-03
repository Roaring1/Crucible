"""Real PyQt interaction checks, isolated from legacy tests' optional-module stubs."""
import json
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PyQt6.QtCore import QMimeData, QPoint, Qt, QUrl
from PyQt6.QtGui import QDragEnterEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
 QApplication,
 QGroupBox,
 QInputDialog,
 QMenu,
 QMessageBox,
 QPushButton,
 QScrollArea,
 QStatusBar,
)

from crucible.data.fleet_backup import GenerationSummary
from crucible.data.instance_manager import InstanceManager
from crucible.data.instance_model import ServerInstance
from crucible.mods.mod_manager import ModEntry
from crucible.ui import theme
from crucible.ui.add_dialog import AddInstanceDialog
from crucible.ui.add_mod_dialog import AddModDialog, _SearchWorker
from crucible.ui.instance_panel import InstancePanel
from crucible.ui.main_window import MainWindow
from crucible.ui.modpack_dialog import ModpackDialog, _PackInstallWorker
from crucible.ui.new_server_dialog import NewServerDialog
from crucible.ui.sidebar import Sidebar
from crucible.ui.tabs.backup_tab import BackupTab
from crucible.ui.tabs.console_tab import ConsoleTab
from crucible.ui.tabs.mods_tab import ModsTab
from crucible.ui.tabs.players_tab import PlayersTab

app=QApplication([]);app.setStyleSheet(theme.STYLESHEET)

def focusable_in_tab_order(root_widget):
 seen=set();ordered=[];widget=root_widget
 while widget not in seen and len(ordered)<200:
  seen.add(widget)
  if (
   widget.isVisible()
   and widget.isEnabled()
   and widget.focusPolicy() & Qt.FocusPolicy.TabFocus
  ):
   ordered.append(widget)
  widget=widget.nextInFocusChain()
 return ordered

root=Path(tempfile.mkdtemp(prefix='crucible-qt-test-'))
inst=ServerInstance(str(root/'server'),'Test server',minecraft_version='1.21.1',loader='fabric',loader_version='0.16.10')
rail=Sidebar();rail.populate([inst],{inst.id:'stopped'})
rail_item=rail._items[inst.id];rail_widget=rail._row_widgets[inst.id]
assert rail_item.text()=='' and rail_item.icon().isNull()
assert rail_widget._status_label.text()=='Stopped'
assert rail_widget.minimumHeight() >= 44
assert rail_widget.accessibleName()=='Test server, status Stopped'
assert rail_widget._status_label.accessibleName()=='Server status: Stopped'
rail.update_status(inst.id,'running')
assert rail_widget._status_label.text()=='Running'
assert rail_item.text()=='' and rail_item.icon().isNull(), 'status polling must not restore duplicate item text/icons'
rail.close()
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
mods._instance=inst;mods._manager=MagicMock();mods._populate_table([first,second])
assert mods._table.columnCount()==4
assert [mods._table.horizontalHeaderItem(i).text() for i in range(4)]==['State','Mod','Version','Runs on']
assert mods._table.currentRow()>=0
assert mods._change_selected_btn.isEnabled() and mods._remove_selected_btn.isEnabled()
assert mods._table.cellWidget(0,mods._COL_ENABLED).text() in {'Enabled','Disabled'}
assert not any(button.text().strip()=='Delete' for button in mods._table.findChildren(QPushButton))
assert mods.findChild(__import__('PyQt6.QtWidgets',fromlist=['QScrollArea']).QScrollArea) is not None
context_actions=[]
with patch.object(QMenu,'exec',lambda menu,*args:context_actions.extend(action.text() for action in menu.actions())):
 rect=mods._table.visualItemRect(mods._table.item(0,mods._COL_NAME))
 mods._show_context_menu(rect.center())
assert 'Delete' not in context_actions, context_actions
assert 'Change version / downgrade…' not in context_actions, context_actions
group_titles={group.title() for group in mods.findChildren(QGroupBox)}
assert {'Installed mod inventory','Server compatibility','Whole server pack'} <= group_titles
mods._table.sortItems(mods._COL_NAME)
first.name='Zeta';first.version='2';first.side='client'
mods._on_inspect_result(mods._inspect_generation,0,first)
for row in range(mods._table.rowCount()):
 item=mods._table.item(row,mods._COL_NAME)
 if item.data(Qt.ItemDataRole.UserRole) is first:
  assert item.text()=='Zeta' and 'Client only' in mods._table.item(row,mods._COL_SIDE).text()
  assert mods._table.item(row,mods._COL_VERSION).toolTip()=='2'
mods._filter.setText('z.jar');app.processEvents()
assert any(
 not mods._table.isRowHidden(row)
 and mods._table.item(row,mods._COL_NAME).data(Qt.ItemDataRole.UserRole) is first
 for row in range(mods._table.rowCount())
), 'mod filter must match filenames as well as display names'
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

m=InstanceManager(root/'registry');m.load();(root/'server').mkdir()
empty_manager=InstanceManager(root/'empty-registry');empty_manager.load()
empty_panel=InstancePanel(empty_manager);empty_panel.resize(900,560);empty_panel.show();QTest.qWait(20)
assert empty_panel._empty_title.text()=='No servers yet'
assert empty_panel._empty_state.isVisible() and empty_panel._tabs.isHidden() and empty_panel._header.isHidden()
assert not empty_panel._btn_start.isVisible() and not empty_panel._btn_ip.isVisible()
empty_panel.close();empty_panel.deleteLater();QTest.qWait(10)
inst=m.add_instance(str(root/'server'),'Before',minecraft_version='1.21.1',loader='fabric')

# The fleet backup workspace exposes restore as a clearly scoped action and
# names its non-overwrite boundary. Preview with synthetic generation data;
# never execute the restore worker or touch a real backup generation here.
preview_generation=GenerationSummary(
 '20261103T000000000000Z-preview00',
 root/'preview-fleet'/'generations'/'preview00',
 '2026-11-03T00:00:00Z',
 1,4,4096,4096,
 server_names=('Before',),
 world_saves=('Before: world [active]',),
)
preview_backup_data={
 'generations':[preview_generation],
 'issues':[],
 'state':{'last_status':'success','last_completed_at_utc':'2026-11-03T00:00:00Z','registered_servers':1},
 'scheduler':{'active':'false','enabled':'false','next':'','detail':'synthetic preview'},
}
with patch.object(
 BackupTab,'_run_job',
 lambda tab,mode: tab._render_inventory(preview_backup_data),
):
 backup_ui=BackupTab(m)
 backup_ui.load(inst)
assert not backup_ui._restore_btn.isHidden()
assert backup_ui._restore_btn.isEnabled()
assert backup_ui._restore_btn.accessibleName()=='Restore selected server snapshot to a new folder'
assert backup_ui._open_btn.accessibleName()=='Open Crucible fleet backup folder'
assert backup_ui._timer_btn.accessibleName()=='Enable ten-minute automatic fleet backups'
assert backup_ui._timer_remove_btn.accessibleName()=='Disable automatic fleet backup schedule'
assert backup_ui._backup_btn.accessibleName()=='Back up all registered servers now'
assert backup_ui._verify_btn.accessibleName()=='Deep verify both retained fleet generations'
assert backup_ui._table.accessibleName()=='Retained complete fleet backup generations'
assert 'live registered server is never overwritten' in backup_ui._restore_btn.toolTip().lower()
active_timer_preview={**preview_backup_data,'scheduler':{
 'active':'true','enabled':'true','next':'14:10','detail':'timer active'
}}
backup_ui._render_inventory(active_timer_preview)
assert backup_ui._timer_btn.text()=='Refresh timer status'
assert backup_ui._timer_btn.accessibleName()=='Refresh automatic fleet backup timer status'
assert backup_ui._timer_remove_btn.isEnabled()
blocked_timer_preview={**preview_backup_data,
 'legacy_backup_blockers':[{'path':'/legacy/orphan','reason':'unassigned server identity'}]
}
backup_ui._render_inventory(blocked_timer_preview)
assert not backup_ui._timer_btn.isEnabled()
assert backup_ui._timer_btn.accessibleName()==(
 'Automatic backups unavailable until legacy sources are migrated'
)
backup_ui._render_inventory(preview_backup_data)
assert backup_ui._timer_btn.accessibleName()=='Enable ten-minute automatic fleet backups'
backup_ui.close();backup_ui.deleteLater();QTest.qWait(10)

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
source_scroll=add.findChild(QScrollArea)
assert source_scroll is not None
assert source_scroll.accessibleName()=='Add server source options'
assert source_scroll.focusPolicy()==Qt.FocusPolicy.NoFocus
assert add._import_folder_btn.accessibleName()=='Import Prism instance'
assert add._import_archive_btn.accessibleName()=='Import modpack archive'
assert add._destination_label.accessibleName()=='Import destination parent folder'
assert add._path_edit.accessibleName()=='Existing server folder'
assert add._name_edit.accessibleName()=='Registered server display name'
assert add._ver_edit.accessibleName()=='Server version (optional)'
assert add._session_edit.accessibleName()=='tmux session name (optional)'
assert add._add_button.accessibleName()=='Add selected server to Crucible'
assert add._cancel_button.accessibleName()=='Cancel adding server'
change_import_destination=next(
 button for button in add.findChildren(QPushButton)
 if button.accessibleName()=='Change import destination folder'
)
add_focus_order=focusable_in_tab_order(add)
expected_add_focus_order=(
 add._import_folder_btn,
 add._import_archive_btn,
 change_import_destination,
 add._advanced_toggle,
 add._cancel_button,
)
assert all(widget in add_focus_order for widget in expected_add_focus_order)
assert [add_focus_order.index(widget) for widget in expected_add_focus_order] == sorted(
 add_focus_order.index(widget) for widget in expected_add_focus_order
), 'Add server Tab order must start with the import routes, then destination, registration disclosure and Cancel'
add._import_folder_btn.setFocus();app.processEvents()
for current,next_widget in zip(expected_add_focus_order,expected_add_focus_order[1:]):
 QTest.keyClick(current,Qt.Key.Key_Tab);app.processEvents()
 assert app.focusWidget() is next_widget, (
  f'Add server Tab from {current.accessibleName()} should reach {next_widget.accessibleName()}'
 )
assert not add._advanced_body.isVisible(), 'advanced registration fields should be hidden by default'
assert add._advanced_toggle.accessibleName()=='Show registration fields for an existing server folder'
assert not add._add_button.isEnabled(), 'Add server must not be clickable before an import or folder is selected'
add._advanced_toggle.click();app.processEvents();assert add._advanced_body.isVisible()
assert add._advanced_toggle.accessibleName()=='Hide existing server registration fields'
add._path_edit.setText(str(root/'existing-server'));app.processEvents()
assert add._add_button.isEnabled(), 'choosing a folder should enable Add server'
add._path_edit.clear();app.processEvents()
assert not add._add_button.isEnabled(), 'clearing the folder should disable Add server again'
add._advanced_toggle.click();app.processEvents();assert not add._advanced_body.isVisible()
assert add._advanced_toggle.accessibleName()=='Show registration fields for an existing server folder'
for field in (add._path_edit,add._name_edit,add._ver_edit,add._session_edit):assert field.height()>=32
add.reject()

# Advanced version settings stay out of the way, and creating a server must
# refuse a non-empty destination rather than overwriting files.
with patch.object(NewServerDialog,'_reload_versions',lambda self:None):
 fresh=NewServerDialog(m)
fresh.resize(720,620);fresh.show();QTest.qWait(20)
assert fresh._loader_combo.accessibleName()=='Server software'
assert fresh._ver_combo.accessibleName()=='Minecraft version'
assert fresh._ver_combo.lineEdit().accessibleName()=='Minecraft version'
assert fresh._advanced_toggle.accessibleName()=='Show more server version options'
assert fresh._name_edit.accessibleName()=='Server display name'
assert fresh._path_edit.accessibleName()=='Server destination folder'
assert fresh._lver_edit.accessibleName()=='Loader version'
assert fresh._snap_check.accessibleName()=='Include Minecraft snapshot versions'
assert fresh._eula_check.accessibleName()=='Accept the Minecraft EULA'
assert fresh._cancel_btn.accessibleName()=='Cancel server creation'
assert fresh._create_btn.accessibleName()=='Create server with selected software and folder'
browse_server_folder=next(
 button for button in fresh.findChildren(QPushButton)
 if button.accessibleName()=='Choose server destination folder'
)
focus_order=focusable_in_tab_order(fresh)
expected_focus_order=(
 fresh._loader_combo,
 fresh._ver_combo,
 fresh._ver_combo.lineEdit(),
 fresh._advanced_toggle,
 fresh._name_edit,
 fresh._path_edit,
 browse_server_folder,
 fresh._eula_check,
 fresh._cancel_btn,
 fresh._create_btn,
)
assert all(widget in focus_order for widget in expected_focus_order)
assert [focus_order.index(widget) for widget in expected_focus_order] == sorted(
 focus_order.index(widget) for widget in expected_focus_order
), 'New server keyboard order must follow software/version, optional settings, identity, destination, EULA and final actions'
assert fresh._name_edit.text() == 'My Server'
assert fresh._path_edit.text(), 'default server name should produce a real destination path'
assert not fresh._advanced_body.isVisible()
fresh._advanced_toggle.click();app.processEvents();assert fresh._advanced_body.isVisible()
assert fresh._advanced_toggle.accessibleName()=='Hide advanced version options'
ix=fresh._loader_combo.findText('NeoForge')
if ix >= 0:
 fresh._loader_combo.setCurrentIndex(ix);app.processEvents()
 assert fresh._lver_row_label.isVisible()
fresh._advanced_toggle.click();app.processEvents()
assert not fresh._advanced_body.isVisible()
assert fresh._advanced_toggle.accessibleName()=='Show more server version options'
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
 # The six destinations used during ordinary server operation remain visible.
 # Console is the default landing page; only configuration/reference pages use More.
 panel=window._panel;tabs=panel._tabs;bar=tabs.tabBar()
 def focusable_widgets(root_widget):
  seen=set();ordered=[];current=root_widget
  while current not in seen and len(ordered)<200:
   seen.add(current)
   if current.isVisible() and current.isEnabled() and current.focusPolicy()!=Qt.FocusPolicy.NoFocus:
    ordered.append(current)
   current=current.nextInFocusChain()
  return ordered

 panel._update_status_display('stopped')
 assert panel._status_label.accessibleName().startswith('Selected server status:')
 assert 'SERVER OFFLINE' in panel._status_label.accessibleName()
 sidebar=window._sidebar
 assert sidebar._list.accessibleName()=='Registered servers'
 assert sidebar._appearance_btn.accessibleName()=='Change selected server appearance'
 assert sidebar._add_btn.accessibleName()=='Add server'
 assert panel._btn_ip.accessibleName()=='Copy external server IP address'
 assert panel._btn_start.accessibleName()=='Start selected server'
 assert panel._btn_stop.accessibleName()=='Stop selected server'
 assert tabs.accessibleName()=='Selected server workspace pages'
 assert bar.accessibleName()=='Server workspace navigation'
 assert panel._more_tabs_button.accessibleName()=='More workspace tabs'
 assert 'QToolButton#MoreTabsButton:focus' in panel._more_tabs_button.styleSheet()

 stopped_focus=focusable_widgets(window)
 stopped_order=(
  sidebar._list,sidebar._appearance_btn,sidebar._add_btn,panel._btn_ip,
  panel._btn_start,tabs,bar,panel._console._cmd_input,panel._more_tabs_button
 )
 assert all(widget in stopped_focus for widget in stopped_order)
 assert [stopped_focus.index(widget) for widget in stopped_order] == sorted(
  stopped_focus.index(widget) for widget in stopped_order
 ), 'stopped-state focus order must follow server selection, actions, workspace, Console, then More'
 assert panel._btn_stop not in stopped_focus
 sidebar._add_btn.setFocus();app.processEvents();QTest.keyClick(sidebar._add_btn,Qt.Key.Key_Tab)
 assert app.focusWidget() is panel._btn_ip, 'Tab from Add server should reach the selected-server actions'
 QTest.keyClick(panel._btn_ip,Qt.Key.Key_Tab)
 assert app.focusWidget() is panel._btn_start, 'Tab from Copy IP should reach Start when stopped'
 QTest.keyClick(panel._btn_start,Qt.Key.Key_Tab)
 assert app.focusWidget() is bar, 'Tab from Start should reach the workspace tab bar'
 QTest.keyClick(bar,Qt.Key.Key_Right)
 assert tabs.currentWidget() is panel._mods, 'Right arrow should advance workspace tabs'
 QTest.keyClick(bar,Qt.Key.Key_Left)
 assert tabs.currentWidget() is panel._console, 'Left arrow should return to the Console tab'
 QTest.keyClick(bar,Qt.Key.Key_Tab)
 assert app.focusWidget() is panel._console._view, 'Tab from workspace navigation should enter the Console'

 panel._update_status_display('running')
 assert 'SERVER ONLINE' in panel._status_label.accessibleName()
 running_focus=focusable_widgets(window)
 running_order=(
  sidebar._list,sidebar._appearance_btn,sidebar._add_btn,panel._btn_ip,
  panel._btn_stop,tabs,bar,panel._console._cmd_input,panel._more_tabs_button
 )
 assert all(widget in running_focus for widget in running_order)
 assert [running_focus.index(widget) for widget in running_order] == sorted(
  running_focus.index(widget) for widget in running_order
 ), 'running-state focus order must replace Start with Stop at the same position'
 assert panel._btn_start not in running_focus
 panel._btn_ip.setFocus();app.processEvents();QTest.keyClick(panel._btn_ip,Qt.Key.Key_Tab)
 assert app.focusWidget() is panel._btn_stop, 'Tab from Copy IP should reach Stop when running'
 visible=[tabs.tabText(i) for i in range(tabs.count()) if bar.isTabVisible(i)]
 assert visible==['Console','Mods','World','Backups','Setup','Diagnostics'], visible
 assert tabs.currentWidget() is panel._console
 assert panel._btn_start.height() >= 44 and panel._btn_stop.height() >= 44
 assert all(not bar.isTabVisible(i) for i in (6,7,8))

 # Stop must wait for a confirmed result; cancelling a failed graceful stop
 # restores the running state, while an in-progress/uncertain transition
 # cannot dispatch duplicate lifecycle commands. Start is offered only from
 # a confirmed stopped state. All tmux calls are intercepted below.
 def synchronous_tmux(operation, callback):
  callback(*operation())

 panel._update_status_display('running')
 with patch.object(panel._tmux,'stop',return_value=(False,'simulated graceful-stop failure')), \
      patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Cancel), \
      patch.object(panel,'_run_tmux',new=synchronous_tmux):
  panel._do_stop()
 assert panel._current_status=='running'
 assert panel._btn_stop.isEnabled() and not panel._btn_start.isEnabled()

 panel._update_status_display('stopping')
 assert not panel._btn_stop.isEnabled(), 'Stop must disable during an existing stop transition'
 with patch.object(panel,'_run_tmux') as duplicate_stop:
  panel._do_stop()
 assert not duplicate_stop.called, 'a second Stop must not be dispatched while stopping'

 panel._update_status_display('unknown')
 assert not panel._btn_start.isEnabled() and not panel._btn_stop.isEnabled()
 with patch.object(panel,'_choose_fml_query_result',return_value='confirm'), \
      patch.object(panel,'_preflight_properties'), \
      patch.object(panel,'_run_tmux') as uncertain_start:
  panel._do_start()
  panel._do_stop()
 assert not uncertain_start.called, 'uncertain lifecycle state must not dispatch Start or Stop'

 panel._update_status_display('running')
 with patch.object(panel._tmux,'stop',return_value=(True,'stopped')) as graceful_stop, \
      patch.object(panel._tmux,'start') as unexpected_start, \
      patch.object(panel,'_run_tmux',new=synchronous_tmux):
  panel._do_stop()
 assert graceful_stop.call_count==1
 assert not unexpected_start.called, 'Stop must never chain an automatic Start'
 assert panel._current_status=='stopped'
 assert panel._btn_start.isEnabled() and not panel._btn_stop.isEnabled()

 panel._update_status_display('stopped')
 with patch.object(panel,'_choose_fml_query_result',return_value='confirm'), \
      patch.object(panel,'_preflight_properties'), \
      patch.object(panel,'_run_tmux') as stopped_start:
  panel._do_start()
 assert stopped_start.called, 'Start should be dispatched once the server is confirmed stopped'
 panel._update_status_display('stopped')
 panel._btn_start.setText('▶  Start')
 panel._more_tab_actions[6].trigger();app.processEvents()
 assert tabs.currentWidget() is panel._config
 assert panel._more_tabs_button.text().startswith('More: Config')
 assert panel._more_tab_actions[6].isChecked()
 tabs.setCurrentWidget(panel._setup);app.processEvents()
 assert panel._more_tabs_button.text()=='More ▾'
 tabs.setCurrentWidget(panel._info);app.processEvents()
 assert panel._info._inspect_logs_btn.isEnabled()
 assert panel._info._fix_loading_btn.isEnabled()
 assert not any(button.text().strip() in {'Restart', 'Console'} for button in panel.findChildren(QPushButton))
 assert not window.findChildren(QStatusBar)
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
 assert 'selected existing server' in panel._feedback_label.text().lower()

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
 for btn in (window._panel._btn_start,window._panel._btn_stop):
  assert btn.geometry().right()<=btn.parentWidget().width()
 assert not hasattr(window._panel,'_btn_restart')
 assert not hasattr(window._panel,'_btn_attach')
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
print('Qt workflow smoke passed: navigation, stop/start guards, cancellation recovery, backup restore, keyboard, resizing, workers, provider, rename and drop')

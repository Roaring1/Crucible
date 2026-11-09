"""Real PyQt interaction checks, isolated from legacy tests' optional-module stubs."""
import json
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tmux_isolation import isolate_tmux

isolate_tmux()

from PyQt6.QtCore import QMimeData, QPoint, QSettings, Qt, QUrl
from PyQt6.QtGui import QDragEnterEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
 QApplication,
 QGroupBox,
 QInputDialog,
 QLabel,
 QMenu,
 QMessageBox,
 QPushButton,
 QScrollArea,
 QStatusBar,
 QTabWidget,
)

from crucible.data.instance_manager import InstanceManager
from crucible.data.instance_model import ServerInstance
from crucible.data.world_start_summary import WorldStartSummary
from crucible.mods.mod_manager import ModEntry
from crucible.sharing.history import ShareHistoryStore
from crucible.ui import theme
from crucible.ui.add_dialog import AddInstanceDialog
from crucible.ui.add_mod_dialog import AddModDialog, _SearchWorker
from crucible.ui.client_export_dialog import ClientExportDialog
from crucible.ui.download_dialog import DownloadModsDialog
from crucible.ui.instance_panel import InstancePanel
from crucible.ui.main_window import MainWindow
from crucible.ui.modpack_dialog import ModpackDialog, _PackInstallWorker
from crucible.ui.new_server_dialog import NewServerDialog
from crucible.ui.pack_update_dialog import PackUpdateDialog, RollbackDialog
from crucible.ui.properties_dialog import PropertiesFixDialog
from crucible.ui.share_history_dialog import ShareHistoryDialog
from crucible.ui.share_players_dialog import SharePlayersDialog
from crucible.ui.sidebar import Sidebar
from crucible.ui.tabs.console_tab import ConsoleTab
from crucible.ui.tabs.mods_tab import ModsTab
from crucible.ui.tabs.players_tab import PlayersTab, _PlayerListWidget, _TeleportDialog

app=QApplication([]);app.setStyleSheet(theme.STYLESHEET)

def wait_for_identity_transition(window, timeout=2.0):
 deadline=time.monotonic()+timeout
 while window._identity_transition is not None and time.monotonic()<deadline:
  app.processEvents()
  QTest.qWait(10)
 app.processEvents()
 assert window._identity_transition is None, "server identity animation did not finish before timeout"

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
  browser=AddModDialog(inst);assert browser._provider.count()==1
  assert browser._provider.currentText()=='Modrinth'
  browser.show();QTest.qWait(20)
  assert browser._provider.accessibleName()=='Mod provider'
  assert browser._search.accessibleName()=='Search mods by name or project'
  assert browser._search_btn.accessibleName()=='Search selected mod provider'
  assert browser._cb_client.accessibleName()=='Show client-side mods'
  assert browser._cb_server.accessibleName()=='Show server-side mods'
  assert browser._status.accessibleName()=='Mod search status'
  assert browser._results.accessibleName()=='Mod search results'
  assert browser._results.accessibleDescription()
  assert browser._d_title.accessibleName()=='Selected mod title'
  assert browser._d_author.accessibleName()=='Selected mod author'
  assert browser._d_stats.accessibleName()=='Selected mod statistics'
  assert browser._d_compat.accessibleName()=='Selected mod compatibility'
  assert browser._d_desc.accessibleName()=='Selected mod description'
  assert browser._d_deps.accessibleName()=='Selected mod dependencies'
  assert browser._install_btn.accessibleName()=='Install selected mod'
  assert browser._d_link.accessibleName()=='View selected mod project page'
  assert browser._close_btn.accessibleName()=='Close Add Mod dialog'
  add_mod_focus_order=focusable_in_tab_order(browser)
  expected_add_mod_order=(
   browser._provider,browser._search,browser._search_btn,
   browser._cb_client,browser._cb_server,browser._results,
   browser._d_desc,browser._close_btn
  )
  assert all(widget in add_mod_focus_order for widget in expected_add_mod_order)
  assert [add_mod_focus_order.index(widget) for widget in expected_add_mod_order] == sorted(
   add_mod_focus_order.index(widget) for widget in expected_add_mod_order
  ), 'Add Mod focus order must follow provider, search, side filters, results, details and Close'
  browser.reject()
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
second_server=root/'second-server';second_server.mkdir()
(second_server/'server.properties').write_text('server-port=25566\\n',encoding='utf-8')
second_inst=m.add_instance(
 str(second_server),'Second server',minecraft_version='1.21.1',loader='neoforge'
)

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
 pack_ui.show();QTest.qWait(20)
 assert pack_ui._search.accessibleName()=='Search server modpacks'
 assert pack_ui._search_btn.accessibleName()=='Search Modrinth server modpacks'
 assert pack_ui._status.accessibleName()=='Server modpack search status'
 assert pack_ui._results.accessibleName()=='Server modpack search results'
 assert pack_ui._results.accessibleDescription()
 assert pack_ui._eula.accessibleName()=='Accept the Minecraft EULA'
 assert pack_ui._open_btn.accessibleName()=='View selected modpack on Modrinth'
 assert pack_ui._install_btn.accessibleName()=='Install selected server modpack'
 assert pack_ui._progress.accessibleName()=='Server modpack installation progress'
 assert pack_ui._log.accessibleName()=='Server modpack installation log'
 pack_focus_order=focusable_in_tab_order(pack_ui)
 expected_pack_order=(pack_ui._search,pack_ui._search_btn,pack_ui._results,pack_ui._eula)
 assert all(widget in pack_focus_order for widget in expected_pack_order)
 assert [pack_focus_order.index(widget) for widget in expected_pack_order] == sorted(
  pack_focus_order.index(widget) for widget in expected_pack_order
 ), 'Modpack install focus order must follow search, Search, results and EULA'
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
worker.log.connect(worker_logs.append);worker.finished.connect(lambda result, problems: worker_results.append((result, problems)))
with patch('crucible.ui.modpack_dialog.modpack_auto.install_modpack_from_modrinth') as install_call:
 worker.run()
assert not install_call.called and worker_results==[(None, [])]
assert any('Refusing to install into an existing folder' in line for line in worker_logs)
assert race_marker.read_text()=='keep this file'

fresh_worker_target=root/'fresh-worker-target'
fresh_worker= _PackInstallWorker('test-pack-id',str(fresh_worker_target),True,threading.Event())
fresh_worker_results=[];fresh_worker.finished.connect(lambda result, problems: fresh_worker_results.append((result, problems)))
fake_install_result=SimpleNamespace(ok=True,path=str(fresh_worker_target))
with patch('crucible.ui.modpack_dialog.modpack_auto.install_modpack_from_modrinth',return_value=fake_install_result) as install_call:
 fresh_worker.run()
assert install_call.call_args.kwargs['target_must_not_exist'] is True
assert len(fresh_worker_results)==1 and fresh_worker_results[0][0] is fake_install_result
assert isinstance(fresh_worker_results[0][1],list)

# Completion is communicated in the existing status/log UI, not a second popup.
success_target=root/'installed-modpack';success_target.mkdir()
pack_ui._pending=(hit,str(success_target))
ok_result=SimpleNamespace(
 path=str(success_target),ok=True,cancelled=False,failed_reason='',
 minecraft_version='1.21.1',loader='fabric',loader_version='0.16.10',
 summary=lambda:'Installed test pack'
)
pack_ui._on_installed(ok_result, [])
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
pack_ui._on_installed(duplicate_result, [])
assert pack_ui.result_instance.id==inst.id and pack_ui._accept_requested
pack_ui.close();pack_ui.deleteLater();QTest.qWait(20)

# Share/export/download dialogs expose their source, privacy, publication
# and transfer controls. Construct only: never show Share (which can auto-build),
# never click upload/export, and never start the download worker here.
share_ui=SharePlayersDialog(
 inst,manager=m,history_store=SimpleNamespace()
)
assert share_ui.accessibleName()=='Share the selected server client pack with players'
assert share_ui._source.accessibleName()==(
 'Matching working Prism client profile source'
)
assert share_ui._browse_source.accessibleName()==(
 'Choose the matching working Prism client profile'
)
assert share_ui._resourcepacks.accessibleName()==(
 'Include resource pack files from the selected Prism profile'
)
assert share_ui._remember_source.accessibleName()==(
 'Remember the validated Prism client profile for future shares'
)
assert share_ui._use_api_token.accessibleName()==(
 'Use an unsaved owner Gofile API token for this upload only'
)
assert share_ui._api_token.accessibleName()=='Optional owner Gofile API token'
assert share_ui._progress.accessibleName()==(
 'Client pack preparation and upload progress'
)
assert share_ui._status.accessibleName()=='Client pack sharing status'
assert share_ui._review.accessibleName()=='Included files and pre-upload review'
assert share_ui._review_ack.accessibleName()==(
 'Confirm review of the exact client pack file list and privacy findings'
)
assert share_ui._license_ack.accessibleName()==(
 'Confirm mod and configuration distribution rights for these files'
)
assert share_ui._public_ack.accessibleName()==(
 'Confirm the Gofile link is accessible to anyone who receives it'
)
assert share_ui._link.accessibleName()=='Published Gofile client pack link'
share_buttons={
 child.accessibleName():child
 for child in share_ui.findChildren(QPushButton)
 if child.accessibleName()
}
for name in (
 'Build the client pack ZIP locally for review before upload',
 'Open the locally prepared client pack ZIP',
 'View previous client pack shares and upload links',
 'Publish the reviewed client pack ZIP to Gofile',
 'Cancel the current client pack upload',
 'Copy the published Gofile client pack link',
 'Copy instructions for players to import the client pack',
 'Open the published Gofile client pack link',
 'Close Share Client Pack dialog',
):
 assert name in share_buttons, f'Share Client Pack control missing accessible name: {name}'
share_ui.deleteLater();QTest.qWait(10)

history_store=ShareHistoryStore(root/'share-history'/'history.json')
history_ui=ShareHistoryDialog(history_store)
assert history_ui.accessibleName()=='Local player-pack share history'
assert any(
 label.accessibleName()=='Local sharing history privacy and cleanup explanation'
 for label in history_ui.findChildren(QLabel)
)
assert history_ui._table.accessibleName()==(
 'Previous player-pack shares and upload history'
)
assert history_ui._table.accessibleDescription()
assert history_ui._status.accessibleName()=='Local player-pack share history status'
history_buttons={
 child.accessibleName():child
 for child in history_ui.findChildren(QPushButton)
 if child.accessibleName()
}
for name in (
 'Copy the last Gofile link recorded for the selected share',
 "Open the selected local client-pack ZIP from Crucible's player-packs folder",
 'Verify and permanently delete the selected local client-pack ZIP',
 'Forget local share history records only; keep ZIP files and remote links',
 'Close local player-pack share history',
):
 assert name in history_buttons, f'Share History action missing accessible name: {name}'
history_ui.reject();history_ui.deleteLater();QTest.qWait(10)

export_ui=ClientExportDialog(inst,manager=m)
assert export_ui.accessibleName()=='Advanced client pack export'
assert export_ui._fmt.accessibleName()=='Client pack archive format'
assert export_ui._cfg.accessibleName()==(
 'Include server config and KubeJS files in client export'
)
assert export_ui._source.accessibleName()==(
 'Optional matching Prism client profile source'
)
assert export_ui._resourcepacks.accessibleName()==(
 'Include resource pack files from the selected Prism profile'
)
assert export_ui._remember_source.accessibleName()==(
 'Remember the selected Prism profile for future client exports'
)
assert export_ui._status.accessibleName()=='Client archive export status'
assert export_ui._export_btn.accessibleName()==(
 'Build the selected server client pack archive'
)
assert export_ui._close_btn.accessibleName()=='Close client pack export dialog'
export_ui.reject();export_ui.deleteLater();QTest.qWait(10)

download_ui=DownloadModsDialog(root/'pack-download-target','Synthetic server')
assert download_ui.accessibleName()==(
 'Download server-side mods from the selected pack index'
)
assert download_ui._bar.accessibleName()=='Server pack mod download progress'
assert download_ui._log.accessibleName()==(
 'Server pack mod download results and missing files'
)
assert download_ui._cancel_btn.accessibleName()==(
 'Cancel server pack mod downloads'
)
assert download_ui._close_btn.accessibleName()==(
 'Close server pack mod download results'
)
download_ui.reject();download_ui.deleteLater();QTest.qWait(10)

# Players tools are constructed only: no commands are sent and no JSON file is
# opened. The Teleport mode switch is exercised in memory.
players_ui=PlayersTab()
assert players_ui._online_list.accessibleName()==(
 'Players currently online on the selected server'
)
assert players_ui._online_list.accessibleDescription()
assert players_ui._online_list.objectName()=='OnlinePlayersList'
assert 'QListWidget#OnlinePlayersList:focus' in players_ui._online_list.styleSheet()
players_lists=players_ui.findChild(QTabWidget)
assert players_lists is not None
assert players_lists.accessibleName()==(
 'Whitelist, operator and banned player lists'
)
teleport_ui=_TeleportDialog('Alex',['Alex','Steve'])
assert teleport_ui.accessibleName()=='Teleport player Alex'
assert teleport_ui._coord_radio.accessibleName()==(
 'Teleport Alex to coordinates and a dimension'
)
assert teleport_ui._player_radio.accessibleName()==(
 'Teleport Alex to another online player'
)
assert teleport_ui._dim_combo.accessibleName()==(
 'Destination dimension for player Alex'
)
assert teleport_ui._x.accessibleName()=='Destination X coordinate for Alex'
assert teleport_ui._y.accessibleName()=='Destination Y coordinate for Alex'
assert teleport_ui._z.accessibleName()=='Destination Z coordinate for Alex'
assert teleport_ui._target_combo.accessibleName()==(
 'Online player destination for Alex'
)
assert teleport_ui._target_combo.itemText(0)=='Steve'
assert teleport_ui._dim_combo.isEnabled()
assert not teleport_ui._target_combo.isEnabled()
teleport_ui._player_radio.setChecked(True)
assert not teleport_ui._dim_combo.isEnabled()
assert teleport_ui._target_combo.isEnabled()
assert teleport_ui.command()=='tp Alex Steve'
teleport_ui.reject();teleport_ui.deleteLater();QTest.qWait(10)

whitelist_ui=_PlayerListWidget('whitelist.json',allow_add=True)
assert whitelist_ui._name_input.accessibleName()==(
 'Player name to add to whitelist.json'
)
assert whitelist_ui._add_btn.accessibleName()=='Add player to whitelist.json'
assert whitelist_ui._table.accessibleName()=='Players in whitelist.json'
assert whitelist_ui._table.accessibleDescription()
whitelist_ui._data=[{'name':'Steve','uuid':'synthetic-uuid'}]
whitelist_ui._refresh_table()
assert whitelist_ui._table.cellWidget(0,2).accessibleName()==(
 'Remove Steve from whitelist.json'
)
assert whitelist_ui._status.accessibleName()=='whitelist.json player list status'
whitelist_ui.deleteLater()
ops_ui=_PlayerListWidget('ops.json',allow_add=True)
assert ops_ui._name_input.accessibleName()=='Player name to add to ops.json'
assert ops_ui._add_btn.accessibleName()=='Add player to ops.json'
ops_ui.deleteLater()
banned_ui=_PlayerListWidget('banned-players.json',allow_add=False)
assert banned_ui._table.accessibleName()=='Players in banned-players.json'
assert not hasattr(banned_ui,'_add_btn')
banned_ui.deleteLater()

# Update/rollback and server.properties repair dialogs expose each risky
# choice by name. These fixtures never start the worker or apply a repair.
update_ui=PackUpdateDialog(inst,manager=m)
assert update_ui.accessibleName()=='Update the selected server modpack'
assert update_ui._source.accessibleName()=='Modpack update source'
assert update_ui._prerelease.accessibleName()==(
 'Include beta and release-candidate modpack versions'
)
assert update_ui._keep_previous.accessibleName()==(
 'Keep the previous server install for rollback'
)
assert update_ui._check_btn.accessibleName()==(
 'Check the selected server modpack and build an update plan'
)
assert update_ui._plan_view.accessibleName()=='Update plan'
assert update_ui._progress.accessibleName()=='Server modpack update progress'
assert update_ui._confirm.accessibleName()=='Type the new version to confirm'
assert update_ui._apply_btn.accessibleName()==(
 'Install the reviewed update for the selected server modpack'
)
assert update_ui._stop_btn.accessibleName()=='Stop the current modpack update safely'
assert update_ui._close_btn.accessibleName()=='Close modpack update dialog'
update_ui.close();update_ui.deleteLater();QTest.qWait(10)

rollback_ui=RollbackDialog(inst,manager=m)
assert rollback_ui.accessibleName()==(
 'Restore or prune retained server modpack installs'
)
assert rollback_ui._list.accessibleName()=='Kept installs'
assert rollback_ui._keep_world.accessibleName()==(
 'Carry the current live world into the restored older pack'
)
assert rollback_ui._log.accessibleName()=='Rollback details'
assert rollback_ui._progress.accessibleName()==(
 'Server modpack rollback or pruning progress'
)
assert rollback_ui._rollback_btn.accessibleName()==(
 'Restore the selected retained server modpack install'
)
assert rollback_ui._keep_spin.accessibleName()=='How many old installs to keep'
assert rollback_ui._prune_btn.accessibleName()==(
 'Delete older retained server modpack installs and keep the selected count'
)
assert rollback_ui._close_btn.accessibleName()=='Close rollback dialog'
rollback_ui.close();rollback_ui.deleteLater();QTest.qWait(10)

properties_path=root/'repair-properties'/'server.properties'
properties_path.parent.mkdir()
properties_path.write_text(
 'server-port=notaport\\nview-distance=banana\\n',encoding='utf-8'
)
properties_ui=PropertiesFixDialog(properties_path,minecraft_version='1.21.1')
assert properties_ui.accessibleName()=='Selected server properties repair'
assert properties_ui._rows
for issue,combo in properties_ui._rows:
 assert combo.accessibleName()==(
  f'Replacement choice for server property {issue.key}'
 )
assert properties_ui.findChild(QScrollArea).accessibleName()==(
 'Server properties issues and replacement choices'
)
repair_buttons={
 child.accessibleName():child
 for child in properties_ui.findChildren(QPushButton)
 if child.accessibleName()
}
assert 'Cancel server properties repairs' in repair_buttons
assert 'Apply selected server.properties replacements' in repair_buttons
properties_ui.reject();properties_ui.deleteLater();QTest.qWait(10)

with patch.object(MainWindow,'_run_crash_recovery',lambda self:None):
 window=MainWindow(m);window.resize(900,600);window.show();QTest.qWait(80)
 # This broad interaction smoke inspects every page directly. Construct its
 # secondary pages up front in the test harness; the dedicated lazy-tab tests
 # separately assert that production startup leaves these pages deferred.
 panel=window._panel
 for page_index in (1,2,3,4,5,6,7):
  panel._ensure_tab_created(page_index)
 assert panel._tabs.currentIndex()==0
 # Persisted Reduce Motion is tested against a temporary settings file, not
 # the user's actual ~/.config settings.
 original_settings=window._settings
 window._settings=QSettings(str(root/'motion-settings.ini'),QSettings.Format.IniFormat)
 window._reduce_motion_action.setChecked(False)
 window._set_reduce_motion(False)
 assert theme.MOTION_ENABLED
 assert window._reduce_motion_action.isCheckable()
 # Selecting another server carries its real configured icon into the header.
 first_identity=window._panel._identity_icon.pixmap()
 assert first_identity is not None and not first_identity.isNull()
 window._sidebar.select_by_id(second_inst.id)
 assert window._panel.current_instance_id()==second_inst.id
 assert window._identity_transition is not None, (
  'server selection should create the short identity transition'
 )
 assert window._panel._identity_icon.pixmap().isNull(), (
  'the header identity is represented by the travelling icon until arrival'
 )
 wait_for_identity_transition(window)
 second_identity=window._panel._identity_icon.pixmap()
 assert second_identity is not None and not second_identity.isNull()
 second_row=window._sidebar._row_widgets[second_inst.id]
 assert second_row._selected and second_row._selection_progress==1.0
 # Rapidly switch twice while the first icon is still travelling. The older
 # overlay must not restore a stale image over the current server's artwork.
 window._sidebar.select_by_id(inst.id)
 assert window._identity_transition is not None
 QTest.qWait(30)
 window._sidebar.select_by_id(second_inst.id)
 assert window._identity_transition is not None
 wait_for_identity_transition(window)
 assert window._panel._identity_icon.pixmap().toImage()==(
  window._sidebar._row_widgets[second_inst.id]._icon.pixmap().toImage()
 ), 'rapid server changes must leave the latest selected server artwork in the header'
 # Reduce Motion must also cancel an animation already in progress and
 # restore the currently selected identity immediately.
 window._sidebar.select_by_id(inst.id)
 assert window._identity_transition is not None
 window._reduce_motion_action.setChecked(True)
 assert window._reduce_motion and not theme.MOTION_ENABLED
 assert window._identity_transition is None
 assert window._settings.value('ui/reduce_motion',False,type=bool) is True
 first_row=window._sidebar._row_widgets[inst.id]
 assert window._panel._identity_icon.pixmap().toImage()==(
  first_row._icon.pixmap().toImage()
 ), 'reducing motion mid-flight must restore the selected server artwork'
 assert first_row._selected and first_row._selection_progress==1.0
 window._sidebar.select_by_id(second_inst.id)
 assert window._identity_transition is None
 assert window._panel._identity_icon.pixmap().toImage()==(
  window._sidebar._row_widgets[second_inst.id]._icon.pixmap().toImage()
 )
 window._reduce_motion_action.setChecked(False)
 assert theme.MOTION_ENABLED
 assert window._settings.value('ui/reduce_motion',True,type=bool) is False
 window._settings=original_settings
 rail_row=window._sidebar._row_widgets[inst.id]
 assert rail_row.objectName()=='ServerRailRow'
 assert rail_row.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
 assert 'background: transparent' in rail_row.styleSheet()
 assert 'background: transparent' in rail_row._name.styleSheet()
 assert 'background: transparent' in rail_row._status_label.styleSheet()
 assert rail_row._status_label.text()=='Stopped'
 assert not hasattr(rail_row,'_status_icon'), 'server rail should communicate state with text, not a dot glyph'
 assert not hasattr(window._panel,'_dot'), 'selected-server header should not duplicate status with a dot'
 assert 'SERVER OFFLINE' in window._panel._status_label.text()
 # Inspect the Server appearance choice dialog without entering a file picker
 # or selecting either mutation action.
 appearance_capture={}
 def capture_appearance_box(box):
  appearance_capture['window']=box.accessibleName()
  appearance_capture['buttons']={
   button.text():button.accessibleName() for button in box.buttons()
  }
  box.adjustSize();box.show();QTest.qWait(30);app.processEvents()
  for button in box.buttons():
   metrics=button.fontMetrics()
   assert button.width() >= metrics.horizontalAdvance(button.text())+32, (
    f'Server appearance button text clips: {button.text()!r} '
    f'width={button.width()} required={metrics.horizontalAdvance(button.text())+32}'
   )
   assert button.height() >= metrics.height()+12, (
    f'Server appearance button height clips: {button.text()!r}'
   )
  parent=box.parentWidget()
  box.close()
  if parent is not None:
   parent.activateWindow();parent.raise_();app.setActiveWindow(parent)
   QTest.qWait(20);app.processEvents()
  return 0
 with patch.object(QMessageBox,'exec',capture_appearance_box):
  window._on_appearance_requested(inst)
 assert appearance_capture['window']=='Change selected server appearance'
 assert appearance_capture['buttons']['Choose custom image…']==(
  'Choose a custom image for the selected server'
 )
 assert appearance_capture['buttons']['Use pack image / built-in icon']==(
  'Use the detected server pack image or a built-in icon'
 )
 assert appearance_capture['buttons']['Cancel']=='Cancel server appearance change'
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
 assert not bar.drawBase(), 'native QTabBar base must not draw a stray line beside More'
 assert panel._more_tabs_button.accessibleName()=='More workspace tabs'
 assert 'QToolButton#MoreTabsButton:focus' in panel._more_tabs_button.styleSheet()
 mods=panel._mods
 # MainWindow loads the selected server's mod inventory asynchronously. Wait
 # for that initial read and chunked render before replacing it with the
 # synthetic rows used by the rest of this interaction smoke.
 mods_load_deadline=time.monotonic()+5.0
 while (
  mods._inventory_thread is not None
  or mods._inventory_refresh_pending
  or mods._table_rendering
 ):
  app.processEvents()
  if time.monotonic()>=mods_load_deadline:
   raise AssertionError('initial Mods inventory did not settle before interaction smoke')
  QTest.qWait(5)
 assert not mods.has_active_operation()
 assert mods._filter.accessibleName()=='Filter installed mods by name, ID or filename'
 assert mods._table.accessibleName()=='Installed mods for the selected server'
 assert mods._table.accessibleDescription()
 assert mods._detail_title.accessibleName()=='Selected mod title'
 assert mods._detail_summary.accessibleName()=='Selected mod file details'
 assert mods._change_selected_btn.accessibleName()=='Change the selected mod version'
 assert mods._remove_selected_btn.accessibleName()=='Remove only the selected mod'
 mods._instance=inst
 mods._populate_table([
  ModEntry(
   filename='Example-Mod.jar',
   path=Path('/tmp/Example-Mod.jar'),
   enabled=True,
   size_bytes=128,
   name='Example Mod',
   version='1.0',
  )
 ])
 enabled_state=mods._table.cellWidget(0,mods._COL_ENABLED)
 assert enabled_state.accessibleName()=='Disable Example Mod'
 assert 'QPushButton#ModEnabledButton:focus' in enabled_state.styleSheet()
 mods_actions={
  child.accessibleName():child
  for child in mods.findChildren(QPushButton)
  if child.accessibleName()
 }
 for name in (
  'Add a local JAR to the selected server',
  'Find and install a compatible mod online',
  'Identify installed mods using exact provider file matches',
  'Verify dependencies of installed mods',
  'Refresh installed mod inventory',
  'Describe the selected server Minecraft and loader versions',
  'Build and share the selected server client pack',
  'Export the selected server client pack to an archive',
  'Update the entire selected server modpack',
 ):
  assert name in mods_actions, f'Mods page action missing a scoped accessible name: {name}'

 console=panel._console
 assert console._view.accessibleName()=='Selected server live console output'
 assert console._find_input.accessibleName()=='Find text in console output'
 assert console._cmd_input.accessibleName()=='Send a command to the selected server'
 assert console._tps_label.accessibleName()=='Current selected server ticks per second'
 assert console._players_label.accessibleName()=='Players currently connected to selected server'
 assert console._server_state_label.accessibleName()=='Selected server lifecycle status'
 assert console._suggestions.accessibleName().startswith('Command suggestions;')
 copy_log_button=next(
  child for child in console.findChildren(QPushButton)
  if child.objectName()=='ConsoleCopyLogButton'
 )
 assert copy_log_button.accessibleName()=='Copy visible console output'
 assert copy_log_button.width()>=copy_log_button.sizeHint().width(), (
  f'Copy log button is narrower than Qt requires: '
  f'{copy_log_button.width()} < {copy_log_button.sizeHint().width()}'
 )
 console_actions={
  child.accessibleName():child
  for child in console.findChildren(QPushButton)
  if child.accessibleName()
 }
 for name in (
  'Send command to the selected server',
  'Clear visible console output',
  'Open the selected server log file',
  'Open the selected server tmux session in a terminal',
  'Copy visible console output',
  'Find previous match','Find next match','Close find bar'
 ):
  assert name in console_actions, f'Console action missing a semantic name: {name}'
 terminal_action=console_actions[
  'Open the selected server tmux session in a terminal'
 ]
 assert terminal_action.text()=='Open terminal'
 assert terminal_action.toolTip().startswith(
  "Open this server's managed tmux session"
 )
 assert console._autoscroll_cb.accessibleName()=='Automatically follow new console output'
 assert console._hide_perf_cb.accessibleName()=='Hide repeating TPS poll replies from console output'

 world=panel._world
 assert world._world_label.accessibleName()=='Selected server active world'
 assert world._seed_btn.accessibleName()=='Change the active world seed for newly generated chunks'
 assert world._reset_btn.accessibleName()=='Reset the active world after a verified fleet checkpoint'
 assert world._wipe_btn.accessibleName()=='Permanently wipe the active world after a verified fleet checkpoint'
 assert world._table.accessibleName()=='Worlds and recovery points', repr(world._table.accessibleName())
 assert world._table.columnCount()==6
 assert world._table.accessibleDescription()
 assert world._recovery_state.accessibleName()=='Automatic backup status'
 assert world._recovery_last.accessibleName()=='Last completed backup'
 assert world._backup_all_btn.accessibleName()=='Back up all registered servers now'
 assert world._verify_backups_btn.accessibleName()=='Verify retained automatic backups'
 assert world._restore_server_btn.accessibleName()==(
  'Restore a complete server backup to a new folder'
 )
 assert world._schedule_btn.accessibleName()=='Pause automatic backups'
 assert world._open_folder_btn.accessibleName()=='Open Crucible backup storage folder'
 assert world._cleanup_btn.accessibleName()==(
  'Create and verify a fleet snapshot before cleaning old world copies'
 )
 assert world._dims_toggle_btn.accessibleName()=='Show all world dimensions'
 diagnostics=panel._info
 world._dims_expanded=True;world._set_dims_text([SimpleNamespace(name=f'DIM{i}') for i in range(8)])
 assert world._dims_toggle_btn.accessibleName()=='Show fewer world dimensions'
 world._dims_expanded=False;world._set_dims_text([SimpleNamespace(name=f'DIM{i}') for i in range(8)])
 assert world._dims_toggle_btn.accessibleName()=='Show all world dimensions'
 assert diagnostics._inspect_logs_btn.accessibleName()=='Inspect server logs and crash reports'
 assert diagnostics._fix_loading_btn.accessibleName()=='Check for safe server loading repairs'

 config=panel._config
 assert config._filter_edit.accessibleName()=='Filter server properties by name or value'
 assert config._save_btn.accessibleName()=='Save selected server properties'
 assert config._table.accessibleName()=='Selected server properties'
 assert config._table.accessibleDescription()
 config_actions={
  child.accessibleName():child
  for child in config.findChildren(QPushButton)
  if child.accessibleName()
 }
 assert 'Reload server properties' in config_actions

 setup=panel._setup
 setup.load(inst)
 assert setup._ip_btn.accessibleName()=='Copy the selected server connection address'
 assert setup._xms_spin.accessibleName()=='Minimum Java heap size Xms'
 assert setup._xms_unit.accessibleName()=='Minimum Java heap size unit'
 assert setup._xmx_spin.accessibleName()=='Maximum Java heap size Xmx'
 assert setup._xmx_unit.accessibleName()=='Maximum Java heap size unit'
 assert setup._fml_combo.accessibleName()=='Forge startup question handling'
 setup_buttons={
  child.accessibleName():child
  for child in setup.findChildren(QPushButton)
  if child.accessibleName()
 }
 for name in (
  'Open the selected server folder',
  'Re-check selected server readiness',
  'Save selected server Java memory settings',
  'Apply suggested Java heap sizes',
  'Download matching dedicated server software',
  'Check the selected server modpack for updates',
  'Update the selected server modpack from a link or file',
  'Roll back the selected server to a retained modpack install',
 ):
  assert name in setup_buttons, f'Setup action missing an accessible name: {name}'
 optional_pack_download=next(
  (
   button for button in setup.findChildren(QPushButton)
   if button.text().startswith("Download server mods")
  ),
  None,
 )
 if optional_pack_download is not None:
  assert optional_pack_download.accessibleName()==(
   'Download the missing server-side files listed by this pack'
  )
 assert setup._mem_status.accessibleName()=='Selected server Java memory settings status'

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
 # Entering Mods lazily loads the selected server's real inventory. Wait for
 # that read and incremental render before later lifecycle checks attempt Start.
 mods_load_deadline=time.monotonic()+5.0
 while (
  panel._mods._inventory_thread is not None
  or panel._mods._inventory_refresh_pending
  or panel._mods._table_rendering
  or panel._mods._thread is not None
 ):
  app.processEvents()
  if time.monotonic()>=mods_load_deadline:
   raise AssertionError('lazy Mods inventory did not settle after workspace navigation')
  QTest.qWait(5)
 assert not panel._mods.has_active_operation()
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
 assert visible==['Console','Mods','Worlds','Setup','Diagnostics'], visible
 assert tabs.currentWidget() is panel._console
 assert panel._btn_start.height() >= 44 and panel._btn_stop.height() >= 44
 assert all(not bar.isTabVisible(i) for i in (5,6,7))

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
 def immediate_world_summary(instance, *, for_start):
  panel._world_summary_generation += 1
  summary = WorldStartSummary(
   world_name=instance.world_root_path().name,
   world_path=instance.world_root_path(),
   state='existing',
   size_bytes=1024,
   newest_file_mtime=1_800_000_000,
   region_files=1,
   player_files=1,
   previous_world_name=instance.extra.get('last_started_world'),
  )
  panel._on_world_start_summary_ready(
   instance.id, panel._world_summary_generation, summary, for_start
  )
 with patch.object(panel,'_choose_fml_query_result',return_value='confirm'), \
      patch.object(panel,'_preflight_properties'), \
      patch.object(panel,'_request_world_start_summary',new=immediate_world_summary), \
      patch.object(panel,'_run_tmux') as stopped_start:
  panel._do_start()
 assert stopped_start.called, 'Start should be dispatched only after selected-world inspection'
 panel._update_status_display('stopped')
 panel._btn_start.setText('Start')
 panel._more_tab_actions[5].trigger();app.processEvents()
 assert tabs.currentWidget() is panel._config
 assert panel._more_tabs_button.text().startswith('More: Config')
 assert panel._more_tab_actions[5].isChecked()
 tabs.setCurrentWidget(panel._setup);app.processEvents()
 assert panel._more_tabs_button.text()=='More'
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
 validation_deadline=time.monotonic()+3.0
 while preview._validation_thread is not None and time.monotonic()<validation_deadline:
  app.processEvents();QTest.qWait(5)
 app.processEvents()
 assert preview._validation_thread is None
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
 drop_validation_threads=[]
 original_validate=ServerInstance.validate
 def record_drop_validation(instance, **kwargs):
  drop_validation_threads.append(threading.get_ident())
  return original_validate(instance, **kwargs)
 with patch.object(ServerInstance,'validate',record_drop_validation):
  window._import_dropped_path(dropdir)
  assert window._drop_validation_thread is not None
  drop_deadline=time.monotonic()+3.0
  while window._drop_validation_thread is not None and time.monotonic()<drop_deadline:
   app.processEvents();QTest.qWait(5)
  app.processEvents()
 assert window._drop_validation_thread is None
 assert drop_validation_threads and all(
  thread_id != threading.main_thread().ident for thread_id in drop_validation_threads
 )
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

import asyncio
import datetime as dt
import json
from pathlib import Path
import tempfile
import tkinter as tk
from types import SimpleNamespace as N
import unittest
from unittest.mock import AsyncMock, patch

from telethon.tl import types
from tgreader.common import now
from tgreader.dialog_picker import DialogIndex, DialogPicker, PAGE_SIZE
from tgreader.wizard import select_groups


def dialog(number, **options):
    entity=options.pop('entity',N(id=number,access_hash=1,megagroup=True,forum=False,username=f'offline_{number}'))
    return N(id=-1000000-number,name=f'Группа Python {number}',entity=entity,is_group=True,is_channel=True,
             date=now()-dt.timedelta(days=number%120),unread_count=number%10,archived=bool(number%2),**options)


class DialogFilterTests(unittest.TestCase):
    def test_search_finds_one_of_6000_by_title_username_link_or_id(self):
        dialogs=[dialog(i) for i in range(1,6001)]
        target=dialogs[-1];target.name='Ёж: обучение Python';target.entity.username='learning_unique'
        index=DialogIndex(dialogs,[])
        for query in ['еж обучение','ЁЖ Python','@learning_unique','https://t.me/learning_unique',str(target.id)]:
            self.assertEqual([row.dialog.id for row in index.filtered(query=query)],[target.id])
        self.assertEqual(len(index.filtered()),6000)

    def test_group_channel_and_forum_are_distinct(self):
        group=dialog(1);forum=dialog(2);forum.entity.forum=True
        channel=dialog(3);channel.is_group=False;channel.entity.megagroup=False
        index=DialogIndex([group,forum,channel],[])
        self.assertEqual(len(index.filtered(kind='Группы')),2)
        self.assertEqual([r.dialog.id for r in index.filtered(kind='Группы с темами')],[forum.id])
        self.assertEqual([r.dialog.id for r in index.filtered(kind='Каналы')],[channel.id])

    def test_archive_unread_and_message_date_filters_combine(self):
        at=now();recent=dialog(1);recent.date=at-dt.timedelta(days=2);recent.unread_count=3
        old=dialog(2);old.date=at-dt.timedelta(days=100);old.archived=True
        missing=dialog(3);missing.date=None
        index=DialogIndex([recent,old,missing],[])
        found=index.filtered(archive='Только архив',activity='За 7 дней',unread_only=True,at=at)
        self.assertEqual([r.dialog.id for r in found],[recent.id])
        self.assertEqual([r.dialog.id for r in index.filtered(activity='Старше 90 дней',at=at)],[old.id])
        self.assertEqual([r.dialog.id for r in index.filtered(activity='Дата неизвестна')],[missing.id])

    def test_unavailable_left_and_migrated_are_visible_only_on_request_and_cannot_be_selected(self):
        group=dialog(1);left=dialog(2);left.entity.left=True
        removed=dialog(3,entity=types.ChannelForbidden(3,1,'Removed',megagroup=True))
        migrated=dialog(4);migrated.entity.migrated_to=types.InputChannel(5,1)
        index=DialogIndex([group,left,removed,migrated],[{'chat_id':left.id}])
        self.assertEqual([row.dialog.id for row in index.filtered()],[group.id])
        self.assertEqual(len(index.filtered(show_unavailable=True)),4)
        for item in [left,removed,migrated]:self.assertFalse(index.toggle(item.id))
        self.assertEqual(index.choices(),[])

    def test_previous_selection_survives_search_and_is_not_limited_to_visible_page(self):
        dialogs=[dialog(i) for i in range(1,6001)]
        index=DialogIndex(dialogs,[{'chat_id':dialogs[0].id}])
        index.toggle(dialogs[-1].id)
        self.assertEqual(len(index.filtered(query=str(dialogs[0].id))),1)
        self.assertEqual(len(index.choices()),2)
        self.assertEqual(len(index.filtered(selected_only=True)),2)

    def test_sorting_and_unknown_dates(self):
        a=dialog(1);a.name='А';a.date=None;a.unread_count=100
        b=dialog(2);b.name='Б';b.date=now();b.unread_count=1
        index=DialogIndex([a,b],[])
        self.assertEqual([r.name for r in index.filtered()],['Б','А'])
        self.assertEqual([r.name for r in index.filtered(sort='Название')],['А','Б'])
        self.assertEqual([r.name for r in index.filtered(sort='Непрочитанные')],['А','Б'])


class DialogWindowTests(unittest.TestCase):
    def setUp(self):
        try:self.root=tk.Tk()
        except tk.TclError as error:self.skipTest(str(error))
    def tearDown(self):
        if hasattr(self,'root') and self.root.winfo_exists():self.root.destroy()

    def test_real_window_pages_6000_chats_and_keeps_hidden_selection(self):
        dialogs=[dialog(i) for i in range(1,6001)]
        picker=DialogPicker(self.root,dialogs,[]);self.root.update()
        self.assertEqual(len(picker.tree.get_children()),PAGE_SIZE)
        picker.change_page(1);self.assertIn('Страница 2 из 20',picker.page_label.get())
        first=picker.tree.get_children()[0];picker.toggle(first)
        self.assertEqual(picker.tree.focus(),first)
        picker.search.set(str(dialogs[-1].id));picker.redraw();self.root.update()
        second=picker.tree.get_children()[0];picker.toggle(second)
        self.assertEqual(len(picker.index.choices()),2)
        self.assertIn('скрыто текущими фильтрами: 1',picker.counter.get())
        picker.finish();self.assertEqual(len(picker.answer),2)
        del self.root

    def test_real_window_never_saves_empty_selection_or_selects_unavailable(self):
        removed=dialog(1);removed.entity.left=True
        picker=DialogPicker(self.root,[removed],[])
        picker.unavailable.set(True);picker.redraw()
        with patch('tgreader.dialog_picker.messagebox.showinfo') as hint,patch('tgreader.dialog_picker.messagebox.showerror') as empty:
            picker.toggle(str(removed.id));picker.finish()
        hint.assert_called_once();empty.assert_called_once()
        self.assertEqual(picker.answer,[])


class SavedSelectionTests(unittest.TestCase):
    def test_edit_groups_reuses_auth_and_preserves_output_and_other_settings(self):
        self.run_selection(cancel=False)
    def test_cancel_edit_groups_preserves_exact_existing_configuration(self):
        self.run_selection(cancel=True)

    def run_selection(self,*,cancel):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);old={'chats':[{'chat_id':-1000001}],'auth_mode':'desktop','auth_file':'offline.enc',
                'account_user_id':123,'timezone':'Europe/Berlin','output_dir':'offline-output','bootstrap_days':3}
            original=json.dumps(old).encode();(root/'config.json').write_bytes(original)
            client=N(connect=AsyncMock(),disconnect=AsyncMock(),get_me=AsyncMock(return_value=N(id=123)))
            picker_result=RuntimeError('Настройка отменена') if cancel else [dialog(2)]
            with patch('tgreader.common.load_config',return_value=dict(old)),patch('tgreader.wizard.state_dir',return_value=root), \
                 patch('tgreader.auth.create_client',return_value=client),patch('tgreader.auth.persist_client') as saved, \
                 patch('tgreader.wizard.setup_dialogs',new=AsyncMock(return_value=[dialog(2)])), \
                 patch('tgreader.wizard.choose_dialogs',side_effect=picker_result if cancel else None,return_value=picker_result):
                if cancel:
                    with self.assertRaisesRegex(RuntimeError,'отменена'):asyncio.run(select_groups())
                else:asyncio.run(select_groups())
            if cancel:self.assertEqual((root/'config.json').read_bytes(),original)
            else:
                cfg=json.loads((root/'config.json').read_text())
                self.assertEqual(cfg['chats'][0]['chat_id'],-1000002)
                self.assertEqual({k:v for k,v in cfg.items() if k!='chats'},{k:v for k,v in old.items() if k!='chats'})
            saved.assert_called_once();client.connect.assert_awaited_once();client.disconnect.assert_awaited_once()


if __name__=='__main__':unittest.main()

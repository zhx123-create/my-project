"""Persona editing and explicit, runtime-only single-window robot controls."""
import queue
import sys
import tkinter as tk
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from reply_persona import validate_persona
from reply_robot import RobotRunner, snapshot_text
from server import normalize_api_base


class RobotMixin:
    def show_robot(self):
        if self.robot is not None:
            self.stop_robot(); return
        if self.busy:
            return
        if not self.active_profile:
            self.status.set('请先新建或选择一个命名会话，再配置该会话的机器人。'); return
        if self.watch.active:
            self.stop_watch('正在配置聊天机器人')
        existing = getattr(self, 'robot_dialog', None)
        if existing is not None and existing.winfo_exists():
            existing.lift(); return
        profile = self.memory.profile(self.active_profile)
        persona = profile['persona']
        dialog = tk.Toplevel(self.root); self.robot_dialog = dialog
        dialog.title('角色与自动回复 · ' + self.active_profile); dialog.transient(self.root)
        panel = ttk.Frame(dialog, padding=16); panel.pack(fill='both', expand=True)
        panel.columnconfigure(1, weight=1)
        ttk.Label(panel, text='人设用于本会话的草稿和机器人回复；自动发送每次运行单独开启。', wraplength=610).grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 8))
        name = tk.StringVar(dialog, value=persona['name'])
        ttk.Label(panel, text='角色名称').grid(row=1, column=0, sticky='w')
        ttk.Entry(panel, textvariable=name).grid(row=1, column=1, sticky='ew', padx=(12, 0))
        editors = {}
        for row, (field, label, value, height) in enumerate((
            ('description', '角色与说话习惯', persona['description'], 3),
            ('examples', '示例句 / 口头禅', persona['examples'], 3),
            ('bot_rules', '允许自动回复什么', profile['bot_rules'], 2),
        ), 2):
            ttk.Label(panel, text=label).grid(row=row, column=0, sticky='nw', pady=5)
            editor = ScrolledText(panel, width=53, height=height, wrap='word', font=('Microsoft YaHei UI' if sys.platform == 'win32' else 'Noto Sans CJK SC', 10))
            editor.grid(row=row, column=1, sticky='ew', padx=(12, 0), pady=5)
            editor.insert('1.0', value); editors[field] = editor
        presets = ttk.Frame(panel); presets.grid(row=5, column=0, columnspan=2, sticky='w', pady=4)
        def preset(label, description):
            name.set(label)
            editors['description'].delete('1.0', 'end'); editors['description'].insert('1.0', description)
        for label, description in (
            ('嘴贫搭子', '虚拟聊天搭子，口语化、机灵、偶尔接梗，回复简短。不刻意讨好，不攻击对方。'),
            ('温柔树洞', '虚拟倾听伙伴，温和耐心，先回应情绪，再自然接话，少说教，不强行提问。'),
            ('冷静管家', '虚拟助手，简洁、有条理、礼貌，依据已有资料答复，不知道就直说。'),
        ):
            ttk.Button(presets, text=label, command=lambda title=label, desc=description: preset(title, desc)).pack(side='left', padx=(0, 8))
        ttk.Label(panel, text='示例只模仿表达，不作为真实经历。原有的“表达风格”仍可组合使用。', wraplength=610).grid(row=6, column=0, columnspan=2, sticky='w', pady=(2, 8))
        contact = tk.StringVar(dialog)
        ttk.Label(panel, text='微信顶部聊天名称').grid(row=7, column=0, sticky='w')
        ttk.Entry(panel, textvariable=contact).grid(row=7, column=1, sticky='ew', padx=(12, 0))
        mode = tk.StringVar(dialog, value='试运行（只生成，不发送）')
        ttk.Label(panel, text='机器人模式').grid(row=8, column=0, sticky='w', pady=6)
        ttk.Combobox(panel, textvariable=mode, values=('试运行（只生成，不发送）', '自动发送文字'), state='readonly').grid(row=8, column=1, sticky='ew', padx=(12, 0), pady=6)
        layout = tk.BooleanVar(dialog, value=False)
        ttk.Checkbutton(panel, text='确认当前绑定聊天中：对方消息在左侧，我的消息在右侧', variable=layout).grid(row=9, column=0, columnspan=2, sticky='w')
        ttk.Label(panel, text='实验功能：需微信公开标题、消息文字、输入框和发送按钮控件。仅截图/OCR可用的窗口暂不能自动发送。\n只处理启动后的新消息，合并连续消息；发送至少间隔10秒。表情仍手动发送。', wraplength=610, foreground='#657089').grid(row=10, column=0, columnspan=2, sticky='w', pady=6)
        note = tk.StringVar(dialog)
        ttk.Label(panel, textvariable=note, wraplength=610, foreground='#7250b5').grid(row=11, column=0, columnspan=2, sticky='w')
        def save():
            try:
                choice = validate_persona({'name': name.get(), 'description': editors['description'].get('1.0', 'end-1c'), 'examples': editors['examples'].get('1.0', 'end-1c')})
                self.save_profile_fields()
                self.memory.update_profile(self.active_profile, persona=choice, bot_rules=editors['bot_rules'].get('1.0', 'end-1c'))
                self.status.set('人设已应用到当前会话；下次生成会结合角色描述和示例句。')
                return True
            except (ValueError, OSError) as error:
                note.set(str(error)); return False
        def start():
            if not save():
                return
            index = self.window_box.current()
            if not 0 <= index < len(self.windows):
                note.set('请先关闭此窗口，用“截取微信”或一键分析选定当前聊天窗口，再回来启动。'); return
            if not contact.get().strip() or len(contact.get().strip()) > 100 or not layout.get():
                note.set('请填写微信顶部显示的聊天名称，并核对左右消息布局。'); return
            config = self.current_api_config()
            try:
                normalize_api_base(config.base, config.allow_http)
                if not config.key or not config.model:
                    raise ValueError('请先在 AI 设置填写密钥和模型。')
            except ValueError as error:
                note.set(str(error)); return
            current = self.memory.profile(self.active_profile)
            current['tone'] = self.tone.get()
            current['history_text'] = self.memory.history_text(self.active_profile)
            self.robot = RobotRunner(self.windows[index], contact.get().strip(), current, config, self.robot_events, dry_run=mode.get().startswith('试运行'))
            dialog.destroy()
            self.stop_watch('已切换到聊天机器人')
            self.set_busy(True, operation='robot')
            self.robot_button.configure(text='暂停机器人')
            self.status.set('正在验证绑定聊天和发送控件，并建立新消息基线；不会回复启动前的旧消息。')
            self.watch_status.set('机器人试运行中 · 不发送' if self.robot.dry_run else '自动回复机器人 · 仅当前绑定聊天')
            self.robot.start()
        actions = ttk.Frame(panel); actions.grid(row=12, column=0, columnspan=2, sticky='ew', pady=(6, 0))
        ttk.Button(actions, text='开始机器人', command=start, style='Accent.TButton').pack(side='right')
        def save_close():
            if save():
                dialog.destroy()
        ttk.Button(actions, text='只保存人设', command=save_close).pack(side='right', padx=8)
        ttk.Button(actions, text='取消', command=dialog.destroy).pack(side='left')
        self.robot_fields = {'name': name, **editors, 'contact': contact, 'mode': mode, 'layout': layout, 'note': note, 'start': start, 'save': save}
        dialog.grab_set(); dialog.lift()

    def stop_robot(self):
        if self.robot is not None:
            self.robot.stop()
            self.robot_button.configure(text='正在暂停…')
            self.watch_status.set('机器人已请求暂停')
            self.status.set('已请求暂停；生成中的结果不会继续发送。若已进入发送阶段，请手动核对微信，不要重复发送。')
            if self.active_operation == 'robot':
                self.set_busy(False)

    def poll_robot(self):
        while True:
            try:
                runner, kind, values = self.robot_events.get_nowait()
            except queue.Empty:
                return
            if runner is not self.robot:
                continue
            if kind == 'sent':
                try:
                    if runner.profile['name'] in self.memory.names():
                        self.memory.confirm_sent(runner.profile['name'], values['text'], confirmed_by_robot=True)
                except (ValueError, OSError):
                    runner.stop()
                    self.status.set('消息已确认发送，但记忆保存失败；机器人已暂停，请检查本地文件。')
            if kind == 'stopped':
                self.robot = None
                self.robot_button.configure(text='聊天机器人')
                if self.active_operation == 'robot':
                    self.set_busy(False)
                self.watch_status.set('聊天机器人已暂停')
                continue
            if runner.cancel.is_set():
                continue
            if kind in ('observed', 'stale'):
                snapshot = values['snapshot']
                text = snapshot_text(snapshot)
                self.context.configure(state='normal'); self.context.delete('1.0', 'end'); self.context.insert('1.0', text); self.context.configure(state='disabled')
                try:
                    self.memory.add_context(runner.profile['name'], text)
                except (ValueError, OSError):
                    self.stop_robot(); self.status.set('会话记忆未能保存，机器人已暂停。'); continue
                change = values.get('change', 'stale')
                self.status.set({'baseline': '已建立基线，只处理此后到达的新消息。', 'pending': '检测到对方新消息，正在等待合并连续消息。', 'own': '检测到自己的消息，本轮不回复。', 'unknown': '最新消息的左右方向不明确，本轮不回复。', 'stale': '生成期间聊天变化，旧回复未发送，正在重新等待新消息。'}.get(change, '正在检查当前聊天。'))
            elif kind == 'generating':
                self.status.set('机器人正在结合人设、背景和最新消息判断是否回复…')
            elif kind == 'error':
                self.status.set(values['message'])
                self.understanding.configure(state='normal'); self.understanding.delete('1.0', 'end'); self.understanding.insert('1.0', '机器人已暂停：\n' + values['message']); self.understanding.configure(state='disabled')
            elif kind in ('draft', 'sent', 'skipped'):
                result = values['result']
                for editor in self.reply_texts:
                    editor.configure(state='normal')
                self.clear_results()
                self.understanding.configure(state='normal'); self.understanding.insert('1.0', result['understanding']); self.understanding.configure(state='disabled')
                self.display_reply_count(1)
                if result['replies']:
                    self.reply_texts[0].insert('1.0', result['replies'][0]['text'])
                    self.reply_labels[0].configure(text='已自动发送' if kind == 'sent' else '试运行草稿 · 未发送')
                else:
                    self.reply_labels[0].configure(text='本轮无需回复')
                self.update_meme_suggestions(result)
                for editor in self.reply_texts:
                    editor.configure(state='disabled')
                self.status.set({'draft': '试运行已生成草稿，没有向微信填写或发送。', 'sent': '已确认回复出现在微信聊天中，并记入已发送历史。', 'skipped': '本轮无需自动回复，原因见 AI 理解。'}[kind])

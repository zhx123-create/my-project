"""Single-chat robot scheduling. A failed or uncertain send is never retried."""
from collections import deque
import copy
import threading
import time

from server import generate, make_messages
from wechat_bot import BotAdapterError, OperationCancellation, call_adapter, validate_snapshot


def snapshot_text(snapshot):
    labels = {'other': '对方', 'self': '我', 'unknown': '说话者未知'}
    return '\n'.join(f"{labels[item['direction']]}：{item['text']}" for item in snapshot['messages'])


class RobotSession:
    def __init__(self, contact, settle_seconds=2, cooldown=10):
        self.contact, self.settle_seconds, self.cooldown = contact, settle_seconds, cooldown
        self.previous = None
        self.pending = None
        self.pending_at = 0
        self.last_sent_at = float('-inf')
        self.sent_count = 0
        self.handled = deque(maxlen=128)

    def observe(self, snapshot, now):
        validate_snapshot(snapshot, self.contact)
        if self.previous is None:
            self.previous = snapshot
            return 'baseline'
        if snapshot['digest'] == self.previous['digest']:
            if self.pending and now - self.pending_at >= self.settle_seconds and now - self.last_sent_at >= self.cooldown:
                return 'ready'
            return 'same'
        old, new = self.previous['messages'], snapshot['messages']
        # Expanding to older history while keeping the same tail is not incoming.
        if len(new) >= len(old) and new[-len(old):] == old:
            self.previous = snapshot
            return 'same'
        overlap = next((size for size in range(min(len(old), len(new)), 0, -1) if old[-size:] == new[:size]), 0)
        if not overlap:
            raise BotAdapterError('消息上下文不连续，机器人已暂停，请核对当前聊天。')
        self.previous = snapshot
        self.pending = None
        if not new[overlap:]:
            return 'same'
        last = new[-1]
        if last['direction'] == 'self':
            return 'own'
        if last['direction'] != 'other':
            return 'unknown'
        if snapshot['digest'] in self.handled:
            return 'same'
        self.pending, self.pending_at = snapshot, now
        return 'pending'

    def complete(self, snapshot, sent=False, now=0):
        self.handled.append(snapshot['digest'])
        self.pending = None
        if sent:
            self.sent_count += 1
            self.last_sent_at = now


class RobotRunner:
    def __init__(self, window, contact, profile, config, events, dry_run=True, interval=5):
        self.window, self.contact = window, contact
        self.profile, self.config = copy.deepcopy(profile), config
        self.events, self.dry_run, self.interval = events, dry_run, interval
        self.cancel = OperationCancellation()
        self.session = RobotSession(contact)

    def emit(self, kind, **values):
        self.events.put((self, kind, values))

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self):
        self.cancel.set()

    def run(self):
        history = self.profile.get('history_text', '')
        try:
            while not self.cancel.is_set():
                snapshot = call_adapter(self.window, self.contact, cancel=self.cancel)
                if self.cancel.is_set():
                    break
                change = self.session.observe(snapshot, time.monotonic())
                if change != 'same':
                    self.emit('observed', snapshot=snapshot, change=change)
                if change == 'ready':
                    candidate = self.session.pending
                    self.emit('generating')
                    messages = make_messages({
                        'context': snapshot_text(candidate), 'history': history[-12000:],
                        'background': self.profile['background'], 'relationship': self.profile['relationship'],
                        'conversation_name': self.profile['name'], 'intent': self.profile['goal'],
                        'style': self.profile['style'], 'persona': self.profile['persona'], 'tone': self.profile.get('tone', '自然'),
                        'bot_rules': self.profile['bot_rules'], 'automated': True,
                        'include_analysis': True, 'reply_count': 1,
                    })
                    result = generate(self.config, messages, reply_count=1, include_analysis=True, automated=True)
                    if self.cancel.is_set():
                        break
                    if not result['should_reply']:
                        self.session.complete(candidate)
                        self.emit('skipped', result=result)
                    else:
                        text = result['replies'][0]['text']
                        fresh = call_adapter(self.window, self.contact, cancel=self.cancel)
                        if self.cancel.is_set():
                            break
                        if fresh['digest'] != candidate['digest']:
                            self.session.observe(fresh, time.monotonic())
                            self.emit('stale', snapshot=fresh)
                        elif self.dry_run:
                            self.session.complete(candidate)
                            self.emit('draft', result=result)
                        else:
                            receipt = call_adapter(self.window, self.contact, action='send', text=text,
                                                   expected_digest=candidate['digest'], cancel=self.cancel)
                            # Record only a confirmed outgoing message, including
                            # one which completed just as the user pressed pause.
                            self.session.complete(candidate, sent=True, now=time.monotonic())
                            self.session.previous = receipt['snapshot']
                            history = (history + '\n用户授权机器人核对已发送（我）：' + text)[-12000:]
                            self.emit('sent', result=result, text=text, snapshot=receipt['snapshot'])
                if self.cancel.wait(self.interval):
                    break
        except Exception as error:
            if not self.cancel.is_set():
                from server import ServiceError
                message = str(error) if isinstance(error, (BotAdapterError, ServiceError)) else '机器人操作未完成，已暂停。请核对微信；不会自动重试发送。'
                self.emit('error', message=message)
        finally:
            self.emit('stopped')

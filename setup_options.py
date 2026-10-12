"""Choose which Claude apps to install and onboard; installation != login."""
import curses
import json
from pathlib import Path
import shutil

from desktop_profiles import atomic_json

CHOICES = ('cli', 'desktop')


def normalize(value):
    if value == 'both':
        return list(CHOICES)
    values = value.split(',') if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)) or not values or any(v not in CHOICES for v in values):
        raise ValueError('Select CLI, Desktop, or both.')
    return [v for v in CHOICES if v in values]


def load(home=None):
    path = (home or Path.home())/'.config/claude-accounts/preferences.json'
    try:
        return normalize(json.loads(path.read_text())['components'])
    except (OSError, ValueError, KeyError, TypeError):
        return list(CHOICES)


def save(components, home=None):
    path = (home or Path.home())/'.config/claude-accounts/preferences.json'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json(path, {'components': normalize(components)})


def detected():
    return {'cli': bool(shutil.which('claude')), 'desktop': Path('/Applications/Claude.app').is_dir()}


class Selector:
    def __init__(self, screen, installed, selected):
        self.screen, self.installed = screen, installed
        self.selected = set(selected)
        self.cursor, self.notice = 0, ''
        self.finished, self.result = False, None
        screen.keypad(True)
        try:
            curses.curs_set(0)
            curses.set_escdelay(25)
        except (curses.error, AttributeError):
            pass
        self.colors = curses.has_colors()
        if self.colors:
            curses.start_color(); curses.use_default_colors()
            for i, pair in enumerate(((6,-1),(7,-1),(2,-1),(7,4),(3,-1)),1):
                curses.init_pair(i,*pair)

    def put(self,y,x,text,role=2,bold=False):
        h,w=self.screen.getmaxyx()
        if 0 <= y < h and x < w-1:
            attr=(curses.color_pair(role) if self.colors else 0) | (curses.A_BOLD if bold else 0)
            try:self.screen.addnstr(y,x,text,max(0,w-x-1),attr)
            except curses.error:pass

    def draw(self):
        self.screen.erase()
        h,w=self.screen.getmaxyx()
        self.put(1,3,'CLAUDE ACCOUNTS  /  SETUP',1,True)
        if h<20 or w<60:
            self.put(3,3,'Enlarge terminal to 60 columns × 20 rows.',5)
            self.put(5,3,'Esc: cancel',1)
        else:
            self.put(3,3,'What would you like to use?',1,True)
            self.put(4,3,'Select one or both. Detected apps will be reused.')
            for i,(key,title,detail) in enumerate((('cli','Claude Code CLI','Use Claude in your terminal.'),
                                                  ('desktop','Claude Desktop','Use the desktop app and saved account switching.'))):
                y=7+i*4
                checked='[x]' if key in self.selected else '[ ]'
                self.put(y,3,('› ' if self.cursor==i else '  ')+checked+' '+title,4 if self.cursor==i else 2,True)
                self.put(y+1,8,'✓ Detected' if self.installed.get(key) else 'Will install' if key in self.selected else 'Not installed',3 if self.installed.get(key) else 2)
                self.put(y+2,8,detail)
            self.put(16,3,('› ' if self.cursor==2 else '  ')+'Continue',4 if self.cursor==2 else 1,True)
            self.put(h-2,3,self.notice or '↑/↓ move   Space: toggle   Enter: continue   Esc: cancel',5 if self.notice else 1)
        self.screen.refresh()

    def key(self,key):
        if key in (27,ord('q')):
            self.finished=True
        elif key in (curses.KEY_UP,ord('k')):self.cursor=(self.cursor-1)%3
        elif key in (curses.KEY_DOWN,ord('j'),9):self.cursor=(self.cursor+1)%3
        elif key==32 and self.cursor<2:
            choice=CHOICES[self.cursor]
            if choice in self.selected:self.selected.remove(choice)
            else:self.selected.add(choice)
            self.notice=''
        elif key in (10,13,curses.KEY_ENTER):
            if not self.selected:self.notice='Select at least one app with Space.'
            elif min(self.screen.getmaxyx())>=20 and self.screen.getmaxyx()[1]>=60:
                self.result=[v for v in CHOICES if v in self.selected]
                self.finished=True

    def run(self):
        while not self.finished:
            self.draw(); self.key(self.screen.getch())
        return self.result


def choose(installed=None, selected=None):
    try:
        return curses.wrapper(lambda screen: Selector(screen, detected() if installed is None else installed,
                                                       load() if selected is None else selected).run())
    except KeyboardInterrupt:
        return None

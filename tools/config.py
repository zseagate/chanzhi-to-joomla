"""Single place for every site-specific setting.

Copy this project, fill in YOUR values below (or export the env vars),
and never commit real dumps, passwords or article content.
Secrets come from environment variables only — there is intentionally
no password file in this repo.
"""
import os

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TOOLS_DIR)
OUT = os.path.join(REPO, 'out')

# --- Source backup (mysqldump of the old Chanzhi site) ---
SRC_SQL = os.environ.get('CHANZHI_SQL', '/path/to/source_mysqldump.sql')
# --- Unpacked old-site web root; image/file lookup happens under <webdata>/ ---
SRC_WEBDATA = os.environ.get('CHANZHI_WEBDATA', '/path/to/unpacked_site/www/data')

# --- Target Joomla database (read via env, never hardcode) ---
JDB_HOST = os.environ.get('JDB_HOST', 'localhost')
JDB_NAME = os.environ.get('JDB_NAME', 'joomla')
JDB_USER = os.environ.get('JDB_USER', 'joomla')
JDB_PASS = os.environ.get('JDB_PASS', '')

# --- Joomla site web root (for copying staged images into place) ---
JOOMLA_ROOT = os.environ.get('JOOMLA_ROOT', '/var/www/html')
# URL prefix rewritten from old /file.php links, e.g. file.php?f=x&t=jpg -> /images/site/x.jpg
IMAGE_BASE = os.environ.get('IMAGE_BASE', '/images/site')

# --- Category mapping (EXAMPLE values — replace with yours) ---
# Old CMS category ids worth keeping, in priority order (used when one
# article belongs to several categories: the first match wins as canonical).
ALIVE_ORDER = ['21', '17', '24', '77']
# Joomla category ids these map to, in the same order:
JCAT_BASE = 1001
# Single-page bucket (old "page" type articles land here):
PAGE_CAT = 1014
# Human names, only used for progress printouts:
CAT_NAMES = {'21': 'News', '17': 'Notices', '24': 'Briefs', '77': 'Training'}

# --- Menus / homepage modules (EXAMPLE values — replace with yours) ---
# (menu title, menu alias, joomla catid)
MENU_CATS = [('News', 'news', '1001'), ('Notices', 'notices', '1002')]
# Categories that need SEF URLs but no nav slot -> hidden menu:
HIDDEN_CATS = [('Archive', 'archive', '1003')]
# Homepage channel blocks, in display order: (title, joomla catid)
CHAN_ORDER = [('News', '1001'), ('Notices', '1002'), ('Archive', '1003')]
# Menu id allocation (must not collide with existing menu ids):
ASSOC_MENU_ID = 1010      # single-page parent (e.g. About)
CAT_MENU_BASE = 1001      # channel menus grow upward from here
HIDDEN_MENU_BASE = 1102   # hidden-route menus grow upward from here
# Single-page dropdown parent, None to skip:
# ASSOC_MENU = {'title': 'About', 'alias': 'about', 'article_id': 2003,
#               'children': [('Intro', 'intro', 2003), ('Charter', 'charter', 2002)]}
ASSOC_MENU = None
# Banner modules from old eps_block table: (module id, title, position, eps_block.id)
BANNER_BLOCKS = []
# Hero background image path inside the old package ('' to skip):
HERO_IMAGE = ''

# --- Single pages refresh (EXAMPLE: old page ids 1-6 -> local 2001-2006) ---
PAGE_IDS = ['1', '2', '3', '4', '5', '6']
PAGE_LOCAL_BASE = 2000

# --- Sitemap page body (EXAMPLE — replace links with yours) ---
SITEMAP_BODY = ('<p><strong>Channels</strong></p><ul><li><a href="/news">News</a></li>'
                '<li><a href="/notices">Notices</a></li></ul>')

# Old site host (stripped to site-relative links; '' to skip):
SOURCE_HOST = os.environ.get('SOURCE_HOST', '')

# --- Screenshot stack ---
GECKODRIVER = os.environ.get('GECKODRIVER', '/tmp/opencode/gd/geckodriver')
FIREFOX_BIN = os.environ.get('FIREFOX_BIN', '/usr/bin/firefox')

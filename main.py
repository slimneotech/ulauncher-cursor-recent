import os
import os.path
import json
import logging
import pathlib
import sqlite3
import urllib
import subprocess
from ulauncher.api.client.Extension import Extension
from ulauncher.api.client.EventListener import EventListener
from ulauncher.api.shared.event import (
	KeywordQueryEvent,
	ItemEnterEvent,
	PreferencesEvent,
	PreferencesUpdateEvent,
)
from ulauncher.api.shared.item.ExtensionResultItem import ExtensionResultItem
from ulauncher.api.shared.item.ExtensionSmallResultItem import ExtensionSmallResultItem
from ulauncher.api.shared.action.RenderResultListAction import RenderResultListAction
from ulauncher.api.shared.action.HideWindowAction import HideWindowAction
from ulauncher.api.shared.action.ExtensionCustomAction import ExtensionCustomAction

try:
	from thefuzz import process, fuzz
except ImportError:
	try:
		from fuzzywuzzy import process, fuzz
	except ImportError:
		import fuzzy as process
		import fuzzy as fuzz

logger = logging.getLogger(__name__)


class Utils:
	@staticmethod
	def get_path(filename, from_home=False):
		base_dir = pathlib.Path.home() if from_home else pathlib.Path(
			__file__).parent.absolute()
		return os.path.join(base_dir, filename)


class Code:
	path_dirs = ("/usr/bin", "/bin", "/snap/bin")
	variants = ("Cursor", "Code", "VSCodium")

	def __init__(self):
		self.installed_path = None
		self.config_path = None
		self.global_state_db = None
		self.storage_json = None

		logger.debug('locating installation and config directories')
		for path in (pathlib.Path(path_dir) for path_dir in Code.path_dirs):
			for variant in Code.variants:
				installed_path = path / variant.lower()
				config_path = pathlib.Path.home() / ".config" / variant
				logger.debug('evaluating installation dir %s and config dir %s',
				             installed_path, config_path)
				if installed_path.exists() and config_path.exists() and (config_path / "User" / "globalStorage" / "storage.json").exists():
					logger.debug('found installation dir %s and config dir %s',
					             installed_path, config_path)
					self.installed_path = installed_path
					self.config_path = config_path
					self.global_state_db = config_path / 'User' / 'globalStorage' / 'state.vscdb'
					self.storage_json = config_path / 'User' / 'globalStorage' / 'storage.json'
					return

		logger.warning('Unable to find Cursor installation and config directory')

	def is_installed(self):
		return bool(self.installed_path)

	def get_recents(self):

		# Current
		if self.global_state_db.exists():
			logger.debug('getting recents from global state database')
			try:
				return self.get_recents_global_state()
			except Exception as e:
				logger.error('getting recents from global state database failed', e)
				if not self.storage_json.exists():
					raise e

		# Legacy
		if self.storage_json.exists():
			logger.debug('getting recents from storage.json (legacy)')
			return self.get_recents_legacy()

	def get_recents_global_state(self):
		logger.debug('connecting to global state database %s', self.global_state_db)
		con = sqlite3.connect(self.global_state_db)
		cur = con.cursor()
		cur.execute(
			'SELECT value FROM ItemTable WHERE key = "history.recentlyOpenedPathsList"')
		json_code, = cur.fetchone()
		paths_list = json.loads(json_code)
		entries = paths_list['entries']
		logger.debug('found %d entries in global state database', len(entries))
		return self.parse_entry_paths(entries)

	def get_recents_legacy(self):
		"""
		For Visual Studio Code Pre versions before 1.64
		:uri https://code.visualstudio.com/updates/v1_64
		"""
		logger.debug('loading storage.json')
		storage = json.load(self.storage_json.open("r"))
		entries = storage["openedPathsList"]["entries"]
		logger.debug('found %d entries in storage.json', len(entries))
		return self.parse_entry_paths(entries)

	@staticmethod
	def parse_entry_paths(entries):
		recents = []
		for path in entries:
			if "folderUri" in path:
				uri = path["folderUri"]
				icon = "folder"
				option = "--folder-uri"
			elif "fileUri" in path:
				uri = path["fileUri"]
				icon = "file"
				option = "--file-uri"
			elif "workspace" in path:
				uri = path["workspace"]["configPath"]
				icon = "workspace"
				option = "--file-uri"
			else:
				logger.warning('entry not recognized: %s', path)
				continue

			label = path["label"] if "label" in path else uri.split("/")[-1]
			recents.append({
				"uri":    uri,
				"label":  label,
				"icon":   icon,
				"option": option,
			})
		return recents

	def open_cursor(self, recent, excluded_env_vars):
		if not self.is_installed():
			return
		# Get the current environment variables
		current_env = os.environ.copy()

		# Remove the environment variables that we don't want to pass to the new process if any
		if excluded_env_vars:
			for env_var in excluded_env_vars.split(','):
				env_to_exclude = env_var.strip()
				if env_to_exclude in current_env:
					del current_env[env_to_exclude]

		# Start the new process with the modified environment
		subprocess.run([self.installed_path, recent['option'], recent['uri']], env=current_env)


class CodeExtension(Extension):
	keyword = None
	excluded_env_vars = None
	code = None
	create_file = None
	exclude_dir = None

	def __init__(self):
		super(CodeExtension, self).__init__()
		self.subscribe(KeywordQueryEvent, KeywordQueryEventListener())
		self.subscribe(ItemEnterEvent, ItemEnterEventListener())
		self.subscribe(PreferencesEvent, PreferencesEventListener())
		self.subscribe(PreferencesUpdateEvent, PreferencesUpdateEventListener())
		self.code = Code()

	def get_ext_result_items(self, query):
		query_raw = query
		query = query.lower() if query else ""
		recents = self.code.get_recents()
		# Filter recents if exclude_dir is set and not empty, supporting comma-separated entries
		if self.exclude_dir:
			exclude_dirs = [d.strip() for d in self.exclude_dir.split(',') if d.strip()]
			if exclude_dirs:
				recents = [c for c in recents if not any(ex in c["uri"] for ex in exclude_dirs)]

		items = []
		data = []

		label_matches = process.extract(query, choices=map(lambda c: c["label"], recents), limit=20, scorer=fuzz.partial_ratio)
		uri_matches = process.extract(query, choices=map(lambda c: c["uri"], recents), limit=20, scorer=fuzz.partial_ratio)

		for match in label_matches:
			recent = next((c for c in recents if c["label"] == match[0]), None)
			if (recent is not None and match[1] > 95):
				data.append(recent)

		for match in uri_matches:
			recent = next((c for c in recents if c["uri"] == match[0]), None)
			existing = next((c for c in data if c["uri"] == recent["uri"]), None)
			if (recent is not None and existing is None):
				data.append(recent)	

		# Remove duplicates from data based on uri, keep first occurrence
		seen = set()
		data = [d for d in data if not (d["uri"] in seen or seen.add(d["uri"]))]

		if query_raw.strip() != "" and self.create_file:
			items.append(
				ExtensionResultItem(
					icon=Utils.get_path(f"images/icon.svg"),
					name=query_raw,
					description=f'{query_raw}',
					on_enter=ExtensionCustomAction({'option': '', 'uri':query_raw}),
				)
			)
		for recent in data[:20]:
			parsed = urllib.parse.urlparse(recent["uri"])
			clean_path = urllib.parse.unquote(parsed.path)
			items.append(
				ExtensionResultItem(
					icon=Utils.get_path(f"images/{recent['icon']}.svg"),
					name=urllib.parse.unquote(recent["label"]),
					description=clean_path,
					on_enter=ExtensionCustomAction(recent),
				)
			)
		return items


class KeywordQueryEventListener(EventListener):
	def on_event(self, event, extension):
		items = []

		if not extension.code.is_installed():
			items.append(
				ExtensionResultItem(
					icon=Utils.get_path("images/icon.svg"),
					name="No Cursor?",
					description="Can't find the Cursor's `code` command in your system :(",
					highlightable=False,
					on_enter=HideWindowAction(),
				)
			)
			return RenderResultListAction(items)

		argument = event.get_argument() or ""
		items.extend(extension.get_ext_result_items(argument))
		return RenderResultListAction(items)


class ItemEnterEventListener(EventListener):
	def on_event(self, event, extension):
		recent = event.get_data()
		extension.code.open_cursor(recent, extension.excluded_env_vars)


class PreferencesEventListener(EventListener):
	def on_event(self, event, extension):
		extension.keyword = event.preferences["code_kw"]
		extension.excluded_env_vars = event.preferences['excluded_env_vars']
		create_file_value = event.preferences.get('create_file', 'false')
		extension.create_file = str(create_file_value).lower() == 'true'
		extension.exclude_dir = event.preferences.get('exclude_dir', '')


class PreferencesUpdateEventListener(EventListener):
	def on_event(self, event, extension):
		if event.id == "code_kw":
			extension.keyword = event.new_value
		if event.id == "excluded_env_vars":
			extension.excluded_env_vars = event.new_value
		if event.id == "create_file":
			extension.create_file = str(event.new_value).lower() == 'true'
		if event.id == "exclude_dir":
			extension.exclude_dir = event.new_value


if __name__ == "__main__":
	CodeExtension().run()

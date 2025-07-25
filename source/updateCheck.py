# A part of NonVisual Desktop Access (NVDA)
# This file is covered by the GNU General Public License.
# See the file COPYING for more details.
# Copyright (C) 2012-2025 NV Access Limited, Zahari Yurukov,
# Babbage B.V., Joseph Lee, Christopher Proß

"""Update checking functionality.
@note: This module may raise C{RuntimeError} on import if update checking for this build is not supported.
"""

from collections.abc import Callable
from datetime import datetime
from typing import (
	Any,
	Dict,
	Optional,
	Self,
	Tuple,
)
from uuid import uuid4

import garbageHandler
import globalVars
import config
import core
from NVDAState import WritePaths

if globalVars.appArgs.secure:
	raise RuntimeError("updates disabled in secure mode")
elif config.isAppX:
	raise RuntimeError("updates managed by Windows Store")
import versionInfo

if not versionInfo.updateVersionType:
	raise RuntimeError("No update version type, update checking not supported")
# Avoid a E402 'module level import not at top of file' warning, because several checks are performed above.
import gui.contextHelp  # noqa: E402
from gui.dpiScalingHelper import DpiScalingHelperMixinWithoutInit  # noqa: E402
import sys  # noqa: E402
import subprocess
import os
import inspect
import threading
import time
import pickle

# #9818: one must import at least urllib.request in Python 3 in order to use full urllib functionality.
import urllib.request
import urllib.parse
import hashlib
import wx
import languageHandler

# Avoid a E402 'module level import not at top of file' warning, because several checks are performed above.
import synthDriverHandler  # noqa: E402
import braille
import gui
from gui import guiHelper
from gui.message import displayDialogAsModal  # noqa: E402
from addonHandler import getCodeAddon, AddonError, getIncompatibleAddons
from addonStore.models.version import (  # noqa: E402
	getAddonCompatibilityMessage,
	getAddonCompatibilityConfirmationMessage,
)
import addonAPIVersion
from logHandler import log, isPathExternalToNVDA
import winKernel
from utils.networking import _fetchUrlAndUpdateRootCertificates
from utils.tempFile import _createEmptyTempFileForDeletingFile
from dataclasses import dataclass

import NVDAState


def __getattr__(attrName: str) -> Any:
	"""Module level `__getattr__` used to preserve backward compatibility."""
	if attrName == "CERT_USAGE_MATCH" and NVDAState._allowDeprecatedAPI():
		log.warning(
			"CERT_USAGE_MATCH is deprecated and will be removed in a future version of NVDA. ",
			stack_info=True,
		)
		from utils.networking import _CERT_USAGE_MATCH as CERT_USAGE_MATCH

		return CERT_USAGE_MATCH
	if attrName == "CERT_CHAIN_PARA" and NVDAState._allowDeprecatedAPI():
		log.warning(
			"CERT_CHAIN_PARA is deprecated and will be removed in a future version of NVDA. ",
			stack_info=True,
		)
		from utils.networking import _CERT_CHAIN_PARA as CERT_CHAIN_PARA

		return CERT_CHAIN_PARA
	if attrName == "UPDATE_FETCH_TIMEOUT_S" and NVDAState._allowDeprecatedAPI():
		log.warning(
			"UPDATE_FETCH_TIMEOUT_S is deprecated and will be removed in a future version of NVDA. ",
			stack_info=True,
		)
		from utils.networking import _FETCH_TIMEOUT_S as UPDATE_FETCH_TIMEOUT_S

		return UPDATE_FETCH_TIMEOUT_S
	raise AttributeError(f"module {repr(__name__)} has no attribute {repr(attrName)}")


#: The URL to use for update checks.
_DEFAULT_CHECK_URL = "https://api.nvaccess.org/nvdaUpdateCheck"
#: The time to wait between checks.
CHECK_INTERVAL = 86400  # 1 day
#: The time to wait before retrying a failed check.
RETRY_INTERVAL = 600  # 10 min
#: The download block size in bytes.
DOWNLOAD_BLOCK_SIZE = 8192  # 8 kb

#: directory to store pending update files
storeUpdatesDir = WritePaths.updatesDir
try:
	os.makedirs(storeUpdatesDir)
except OSError:
	if not os.path.isdir(storeUpdatesDir):
		log.debugWarning("Default download path for updates %s could not be created." % storeUpdatesDir)

#: Persistent state information.
state: Optional[Dict[str, Any]] = None

#: The single instance of L{AutoUpdateChecker} if automatic update checking is enabled,
#: C{None} if it is disabled.
autoChecker: Optional["AutoUpdateChecker"] = None


@dataclass
class UpdateInfo:
	"""Data class representing update information for NVDA."""

	version: str
	"""The version of the update."""

	launcherUrl: str
	"""The URL to download the launcher."""

	apiVersion: str
	"""The API version of the update."""

	launcherHash: str | None = None
	"""The SHA1 hash of the launcher, if available."""

	apiCompatTo: str | None = None
	"""The API version that the update is backward-compatible with, if available."""

	changesUrl: str | None = None
	"""The URL to the changelog, if available."""

	launcherInteractiveUrl: str | None = None
	"""URL to download the update from the NV Access website, if available."""

	@classmethod
	def parseUpdateCheckResponse(cls, data: str) -> Self:
		"""Parses the update response and returns an UpdateInfo object.

		:param data: The raw server response as a UTF-8 decoded string.
		:return: An UpdateInfo object containing the update metadata.
		:raises ValueError: If the response format is invalid.
		"""
		parameters = inspect.signature(cls).parameters
		knownKeys: set[str] = set(parameters)
		requiredKeys: set[str] = {key for key, value in parameters.items() if value.default is value.empty}
		metadata: dict[str, str] = {}
		for line in data.splitlines():
			try:
				key, val = line.split(": ", 1)
			except ValueError:
				raise ValueError(f"Invalid line format in update response: {line}")
			if key in knownKeys:
				metadata[key] = val
			else:
				log.debug(f"Dropping unknown key {key} = {val}.")
		requiredKeys.difference_update(metadata)
		if len(requiredKeys) > 0:
			raise ValueError(f"Missing required key(s): {', '.join(requiredKeys)}")
		return cls(**metadata)


def _getCheckURL() -> str:
	if url := config.conf["update"]["serverURL"]:
		return url
	return _DEFAULT_CHECK_URL


def getQualifiedDriverClassNameForStats(cls):
	"""fetches the name from a given synthDriver or brailleDisplay class, and appends core for in-built code, the add-on name for code from an add-on, or external for code in the NVDA user profile.
	Some examples:
	espeak (core)
	newfon (external)
	eloquence (addon:CodeFactory)
	noBraille (core)
	"""
	name = cls.name
	try:
		addon = getCodeAddon(cls)
	except AddonError:
		addon = None
	if addon:
		return "%s (addon:%s)" % (name, addon.name)
	path = inspect.getsourcefile(cls)
	if isPathExternalToNVDA(path):
		return "%s (external)" % name
	return "%s (core)" % name


def checkForUpdate(auto: bool = False) -> UpdateInfo | None:
	"""Check for an updated version of NVDA.
	This will block, so it generally shouldn't be called from the main thread.

	:param auto: Whether this is an automatic check for updates.
	:return: An UpdateInfo object containing the update metadata, or None if there is no update.
	:raise RuntimeError: If there is an error checking for an update.
	"""
	allowUsageStats = config.conf["update"]["allowUsageStats"]
	# #11837: build version string, service pack, and product type manually
	# because winVersion.getWinVer adds Windows release name.
	winVersion = sys.getwindowsversion()
	winVersionText = "{v.major}.{v.minor}.{v.build}".format(v=winVersion)
	if winVersion.service_pack_major != 0:
		winVersionText += " service pack %d" % winVersion.service_pack_major
		if winVersion.service_pack_minor != 0:
			winVersionText += ".%d" % winVersion.service_pack_minor
	winVersionText += " %s" % ("workstation", "domain controller", "server")[winVersion.product_type - 1]

	params = {
		"autoCheck": auto,
		"allowUsageStats": allowUsageStats,
		"version": versionInfo.version,
		"versionType": versionInfo.updateVersionType,
		"osVersion": winVersionText,
		# Check if the architecture is the most common: "AMD64"
		# Available values of PROCESSOR_ARCHITEW6432 found in:
		# https://docs.microsoft.com/en-gb/windows/win32/winprog64/wow64-implementation-details
		"x64": os.environ.get("PROCESSOR_ARCHITEW6432") == "AMD64",
		"osArchitecture": os.environ.get("PROCESSOR_ARCHITEW6432"),
	}

	if auto and allowUsageStats:
		synthDriverClass = synthDriverHandler.getSynth().__class__
		brailleDisplayClass = braille.handler.display.__class__ if braille.handler else None
		# Following are parameters sent purely for stats gathering.
		#  If new parameters are added here, they must be documented in the userGuide for transparency.
		extraParams = {
			"id": state["id"],
			"language": languageHandler.getLanguage(),
			"installed": config.isInstalledCopy(),
			"synthDriver": getQualifiedDriverClassNameForStats(synthDriverClass)
			if synthDriverClass
			else None,
			"brailleDisplay": getQualifiedDriverClassNameForStats(brailleDisplayClass)
			if brailleDisplayClass
			else None,
			"outputBrailleTable": config.conf["braille"]["translationTable"] if brailleDisplayClass else None,
		}
		params.update(extraParams)

	result = _fetchUrlAndUpdateRootCertificates(
		url=f"{_getCheckURL()}?{urllib.parse.urlencode(params)}",
		# We must specify versionType so the server doesn't return a 404 error and
		# thus cause an exception.
		certFetchUrl=f"{_getCheckURL()}?versionType=stable",
	)

	if result.status_code != 200:
		raise RuntimeError(f"Checking for update failed with HTTP status code {result.status_code}.")

	data = result.content.decode("utf-8")  # Ensure the response is decoded correctly
	# if data is empty, we return None, because the server returns an empty response if there is no update.
	if not data:
		return None
	try:
		parsed_response = UpdateInfo.parseUpdateCheckResponse(data)
	except ValueError:
		raise RuntimeError(
			"The update response is invalid. Ensure the update mirror returns a properly formatted response.",
		)

	return parsed_response


def _setStateToNone(_state):
	_state["pendingUpdateFile"] = None
	_state["pendingUpdateVersion"] = None
	_state["pendingUpdateAPIVersion"] = (0, 0, 0)
	_state["pendingUpdateBackCompatToAPIVersion"] = (0, 0, 0)


def getPendingUpdate() -> Optional[Tuple]:
	"""Returns a tuple of the path to and version of the pending update, if any. Returns C{None} otherwise."""
	try:
		pendingUpdateFile = state["pendingUpdateFile"]
		pendingUpdateVersion = state["pendingUpdateVersion"]
		pendingUpdateAPIVersion = state["pendingUpdateAPIVersion"] or (0, 0, 0)
		pendingUpdateBackCompatToAPIVersion = state["pendingUpdateBackCompatToAPIVersion"] or (0, 0, 0)
	except KeyError:
		_setStateToNone(state)
		return None
	else:
		if pendingUpdateFile and os.path.isfile(pendingUpdateFile):
			return (
				pendingUpdateFile,
				pendingUpdateVersion,
				pendingUpdateAPIVersion,
				pendingUpdateBackCompatToAPIVersion,
			)
		else:
			_setStateToNone(state)
	return None


def isPendingUpdate() -> bool:
	"""Returns whether there is a pending update."""
	return getPendingUpdate() is not None


def executePendingUpdate():
	updateTuple = getPendingUpdate()
	if not updateTuple:
		return
	else:
		_executeUpdate(updateTuple[0])


def _executeUpdate(destPath: str) -> None:
	"""Execute the update process.

	:param destPath: The path to the update executable.
	"""
	if not destPath:
		log.error("destPath must be a non-empty string.", exc_info=True)
		return

	_setStateToNone(state)
	saveState()
	if not core.triggerNVDAExit(core.NewNVDAInstance(destPath, _generate_updateParameters())):
		log.error("NVDA already in process of exiting, this indicates a logic error.")


def _generate_updateParameters() -> str:
	"""Generate parameters to pass to the new NVDA instance for the update process.

	We generate parameters that specify:
	- Whether to install, update a portable copy, or run the launcher.
	- Whether to disable addons.
	- The path to the configuration directory.

	:return: The parameters to pass to the new NVDA instance.
	"""
	executeParams: list[str] = []
	if config.isInstalledCopy():
		executeParams.extend(("--install", "-m"))
	else:
		portablePath = globalVars.appDir
		if os.access(portablePath, os.W_OK):
			executeParams.extend(("--create-portable", "-m", "--portable-path", portablePath))
		else:
			# We can't write to the currently running portable copy's directory, so just run the launcher.
			executeParams.append("--launcher")
	if globalVars.appArgs.disableAddons:
		executeParams.append("--disable-addons")
	# pass the config path to the new instance, so that if a custom config path is in use, it will be inherited.
	# If the default con fig path is in use, the new instance would use it anyway, so there is no harm in passing it.
	executeParams.extend(("--config-path", WritePaths.configDir))
	return subprocess.list2cmdline(executeParams)


class UpdateChecker(garbageHandler.TrackedObject):
	"""Check for an updated version of NVDA, presenting appropriate user interface.
	The check is performed in the background.
	This class is for manual update checks.
	To use, call L{check} on an instance.
	"""

	AUTO = False

	def check(self):
		"""Check for an update."""
		t = threading.Thread(
			name=f"{self.__class__.__module__}.{self.check.__qualname__}",
			target=self._bg,
			daemon=True,
		)
		self._started()
		t.start()

	def _bg(self):
		assert state is not None
		lastCheckDate = datetime.fromtimestamp(state["lastCheck"])
		nowDate = datetime.now()
		if (lastCheckDate.year, lastCheckDate.month) != (nowDate.year, nowDate.month):
			# reset unique ID once a month
			state["id"] = uuid4().hex
		try:
			info = checkForUpdate(self.AUTO)
		except:  # noqa: E722
			log.debugWarning("Error checking for update", exc_info=True)
			self._error()
			return
		self._result(info)
		if info:
			state["dontRemindVersion"] = info.version
		state["lastCheck"] = time.time()
		saveState()
		if autoChecker:
			autoChecker.setNextCheck()

	def _started(self):
		self._progressDialog = gui.IndeterminateProgressDialog(
			gui.mainFrame,
			# Translators: The title of the dialog displayed while manually checking for an NVDA update.
			_("Checking for Update"),
			# Translators: The progress message displayed while manually checking for an NVDA update.
			_("Checking for update"),
		)

	def _error(self):
		if url := config.conf["update"]["serverURL"]:
			tip = pgettext(
				"updateCheck",
				# Translators: A suggestion of what to do when checking for NVDA updates fails and an update mirror is being used.
				# {url} will be replaced with the mirror URL.
				"Make sure you are connected to the internet, and the NVDA update mirror URL is valid.\n"
				"Mirror URL: {url}",
			).format(url=url)
		else:
			tip = pgettext(
				"updateCheck",
				# Translators: Presented when fetching add-on data from the store fails and the default metadata URL is being used.
				"Unable to establish a connection to the NV Access server.",
			)
		message = pgettext(
			"updateCheck",
			# Translators: A message indicating that an error occurred while checking for an update to NVDA.
			# tip will be replaced with a context sensitive suggestion of next steps.
			"Error checking for update.\n{tip}",
		).format(tip=tip)
		wx.CallAfter(self._progressDialog.done)
		self._progressDialog = None
		wx.CallAfter(
			gui.messageBox,
			message,
			# Translators: The title of an error message dialog.
			_("Error"),
			wx.OK | wx.ICON_ERROR,
		)

	def _result(self, info: Optional[UpdateInfo]) -> None:
		wx.CallAfter(self._progressDialog.done)
		self._progressDialog = None
		wx.CallAfter(UpdateResultDialog, gui.mainFrame, info, False)


class AutoUpdateChecker(UpdateChecker):
	"""Automatically check for an updated version of NVDA.
	To use, create a single instance and maintain a reference to it.
	Checks will then be performed automatically.
	"""

	AUTO = True

	def __init__(self):
		self._checkTimer = gui.NonReEntrantTimer(self.check)
		if config.conf["update"]["startupNotification"] and isPendingUpdate():
			secsTillNext = 0  # Display the update message instantly
		else:
			# Set the initial check based on the last check time.
			# #3260: If the system time is earlier than the last check,
			# treat the last check as being right now (so the next will be tomorrow).
			secsSinceLast = max(time.time() - state["lastCheck"], 0)
			# The maximum time till the next check is CHECK_INTERVAL.
			secsTillNext = CHECK_INTERVAL - int(min(secsSinceLast, CHECK_INTERVAL))
		self._checkTimer.Start(secsTillNext * 1000, True)

	def terminate(self):
		self._checkTimer.Stop()
		self._checkTimer = None

	def setNextCheck(self, isRetry=False):
		# #6127: Timers must be manipulated from the main thread.
		wx.CallAfter(self._checkTimer.Stop)
		wx.CallAfter(self._checkTimer.Start, (RETRY_INTERVAL if isRetry else CHECK_INTERVAL) * 1000, True)

	def _started(self):
		log.info("Performing automatic update check")

	def _error(self):
		self.setNextCheck(isRetry=True)

	def _result(self, info: UpdateInfo | None) -> None:
		if not info:
			return
		if info.version == state["dontRemindVersion"]:
			return
		wx.CallAfter(UpdateResultDialog, gui.mainFrame, info, True)


class UpdateResultDialog(
	DpiScalingHelperMixinWithoutInit,
	gui.contextHelp.ContextHelpMixin,
	wx.Dialog,  # wxPython does not seem to call base class initializer, put last in MRO
):
	helpId = "GeneralSettingsCheckForUpdates"

	def __init__(self, parent, updateInfo: UpdateInfo | None, auto: bool) -> None:
		# Translators: The title of the dialog informing the user about an NVDA update.
		super().__init__(parent, title=_("NVDA Update"))

		self.updateInfo = updateInfo
		mainSizer = wx.BoxSizer(wx.VERTICAL)
		sHelper = guiHelper.BoxSizerHelper(self, orientation=wx.VERTICAL)

		remoteUpdateExists = updateInfo is not None
		pendingUpdateDetails = getPendingUpdate()
		canOfferPendingUpdate = (
			isPendingUpdate() and remoteUpdateExists and pendingUpdateDetails[1] == updateInfo.version
		)

		text = sHelper.addItem(wx.StaticText(self))
		bHelper = guiHelper.ButtonHelper(wx.HORIZONTAL)
		if not remoteUpdateExists:
			# Translators: A message indicating that no update to NVDA is available.
			message = _("No update available.")
		elif canOfferPendingUpdate:
			message = _(
				# Translators: A message indicating that an update to NVDA has been downloaded and is ready to be
				# applied.
				"Update to NVDA version {version} has been downloaded and is ready to be applied.",
			).format(version=updateInfo.version)

			self.apiVersion = pendingUpdateDetails[2]
			self.backCompatTo = pendingUpdateDetails[3]
			showAddonCompat = (self.backCompatTo[0] > addonAPIVersion.BACK_COMPAT_TO[0]) and any(
				getIncompatibleAddons(
					currentAPIVersion=self.apiVersion,
					backCompatToAPIVersion=self.backCompatTo,
				),
			)
			if showAddonCompat:
				message += "\n\n" + getAddonCompatibilityMessage()
				confirmationCheckbox = sHelper.addItem(
					wx.CheckBox(
						self,
						label=getAddonCompatibilityConfirmationMessage(),
					),
				)
				confirmationCheckbox.Bind(
					wx.EVT_CHECKBOX,
					lambda evt: self.updateButton.Enable(not self.updateButton.Enabled),
				)
				confirmationCheckbox.SetFocus()
				# Translators: The label of a button to review add-ons prior to NVDA update.
				reviewAddonsButton = bHelper.addButton(self, label=_("&Review add-ons..."))
				reviewAddonsButton.Bind(wx.EVT_BUTTON, self.onReviewAddonsButton)
			self.updateButton = bHelper.addButton(
				self,
				# Translators: The label of a button to apply a pending NVDA update.
				# {version} will be replaced with the version; e.g. 2011.3.
				label=_("&Update to NVDA {version}").format(version=updateInfo.version),
			)
			self.updateButton.Bind(
				wx.EVT_BUTTON,
				lambda evt: self.onUpdateButton(pendingUpdateDetails[0]),
			)
			self.updateButton.Enable(not showAddonCompat)
			bHelper.addButton(
				self,
				# Translators: The label of a button to re-download a pending NVDA update.
				label=_("Re-&download update"),
			).Bind(wx.EVT_BUTTON, self.onDownloadButton)
		else:
			# Translators: A message indicating that an updated version of NVDA is available.
			# {version} will be replaced with the version; e.g. 2011.3.
			message = _("NVDA version {version} is available.").format(version=updateInfo.version)
			bHelper.addButton(
				self,
				# Translators: The label of a button to download an NVDA update.
				label=_("&Download update"),
			).Bind(wx.EVT_BUTTON, self.onDownloadButton)
			if auto:  # this prompt was triggered by auto update checker
				# the user might not want to wait for a download right now, so give the option to be reminded later.
				# Translators: The label of a button to remind the user later about performing some action.
				remindMeButton = bHelper.addButton(self, label=_("Remind me &later"))
				remindMeButton.Bind(wx.EVT_BUTTON, self.onLaterButton)
				remindMeButton.SetFocus()

		text.SetLabel(message)
		text.Wrap(self.scaleSize(500))
		sHelper.addDialogDismissButtons(bHelper)

		# Translators: The label of a button to close a dialog.
		closeButton = bHelper.addButton(self, wx.ID_CLOSE, label=_("&Close"))
		closeButton.Bind(wx.EVT_BUTTON, lambda evt: self.Close())
		self.Bind(wx.EVT_CLOSE, lambda evt: self.Destroy())
		self.EscapeId = wx.ID_CLOSE

		mainSizer.Add(sHelper.sizer, border=guiHelper.BORDER_FOR_DIALOGS, flag=wx.ALL)
		self.Sizer = mainSizer
		mainSizer.Fit(self)
		self.CentreOnScreen()
		self.Show()

	def onUpdateButton(self, destPath):
		_executeUpdate(destPath)
		self.Destroy()

	def onDownloadButton(self, evt):
		self.Hide()
		DonateRequestDialog(gui.mainFrame, self._download)

	def _download(self):
		UpdateDownloader(self.updateInfo).start()
		self.Destroy()

	def onLaterButton(self, evt):
		state["dontRemindVersion"] = None
		saveState()
		self.Close()

	def onReviewAddonsButton(self, evt):
		from gui import addonGui

		incompatibleAddons = addonGui.IncompatibleAddonsDialog(
			parent=self,
			APIVersion=self.apiVersion,
			APIBackwardsCompatToVersion=self.backCompatTo,
		)
		displayDialogAsModal(incompatibleAddons)


class UpdateAskInstallDialog(
	DpiScalingHelperMixinWithoutInit,
	gui.contextHelp.ContextHelpMixin,
	wx.Dialog,  # wxPython does not seem to call base class initializer, put last in MRO
):
	helpId = "GeneralSettingsCheckForUpdates"

	def __init__(self, parent, destPath, version, apiVersion, backCompatTo):
		self.destPath = destPath
		self.version = version
		self.apiVersion = apiVersion
		self.backCompatTo = backCompatTo
		self.storeUpdatesDirWritable = os.path.isdir(storeUpdatesDir) and os.access(storeUpdatesDir, os.W_OK)
		# Translators: The title of the dialog asking the user to apply an NVDA update.
		super().__init__(parent, title=_("NVDA Update"))
		mainSizer = wx.BoxSizer(wx.VERTICAL)
		sHelper = guiHelper.BoxSizerHelper(self, orientation=wx.VERTICAL)
		# Translators: A message indicating that an update to NVDA is ready to be applied.
		message = _("Update to NVDA version {version} is ready to be applied.\n").format(version=version)

		showAddonCompat = (self.backCompatTo[0] > addonAPIVersion.BACK_COMPAT_TO[0]) and any(
			getIncompatibleAddons(
				currentAPIVersion=self.apiVersion,
				backCompatToAPIVersion=self.backCompatTo,
			),
		)
		if showAddonCompat:
			message += "\n" + getAddonCompatibilityMessage()
		text = sHelper.addItem(wx.StaticText(self, label=message))
		text.Wrap(self.scaleSize(500))

		if showAddonCompat:
			self.confirmationCheckbox = sHelper.addItem(
				wx.CheckBox(
					self,
					label=getAddonCompatibilityConfirmationMessage(),
				),
			)

		bHelper = sHelper.addDialogDismissButtons(guiHelper.ButtonHelper(wx.HORIZONTAL))
		if showAddonCompat:
			# Translators: The label of a button to review add-ons prior to NVDA update.
			reviewAddonsButton = bHelper.addButton(self, label=_("&Review add-ons..."))
			reviewAddonsButton.Bind(wx.EVT_BUTTON, self.onReviewAddonsButton)
		# Translators: The label of a button to update NVDA.
		updateButton = bHelper.addButton(self, wx.ID_OK, label=_("&Update now"))
		updateButton.Bind(wx.EVT_BUTTON, self.onUpdateButton)
		if not showAddonCompat:
			updateButton.SetFocus()
		else:
			self.confirmationCheckbox.SetFocus()
			self.confirmationCheckbox.Bind(
				wx.EVT_CHECKBOX,
				lambda evt: updateButton.Enable(not updateButton.Enabled),
			)
			updateButton.Enable(False)
		if self.storeUpdatesDirWritable:
			# Translators: The label of a button to postpone an NVDA update.
			postponeButton = bHelper.addButton(self, wx.ID_CLOSE, label=_("&Postpone update"))
			postponeButton.Bind(wx.EVT_BUTTON, self.onPostponeButton)
			self.EscapeId = wx.ID_CLOSE
		else:
			self.EscapeId = wx.ID_OK

		mainSizer.Add(sHelper.sizer, border=guiHelper.BORDER_FOR_DIALOGS, flag=wx.ALL)
		self.Sizer = mainSizer
		mainSizer.Fit(self)
		self.CentreOnScreen()

	def onReviewAddonsButton(self, evt):
		from gui import addonGui

		incompatibleAddons = addonGui.IncompatibleAddonsDialog(
			parent=self,
			APIVersion=self.apiVersion,
			APIBackwardsCompatToVersion=self.backCompatTo,
		)
		displayDialogAsModal(incompatibleAddons)

	@property
	def callback(self) -> Callable[[int], None]:
		"""A callback method which either performs or postpones the update, based on the passed return code."""
		return self._callbackFactory(
			destPath=self.destPath,
			version=self.version,
			apiVersion=self.apiVersion,
			backCompatTo=self.backCompatTo,
		)

	@staticmethod
	def _callbackFactory(
		destPath: str,
		version: str,
		apiVersion: addonAPIVersion.AddonApiVersionT,
		backCompatTo: addonAPIVersion.AddonApiVersionT,
	) -> Callable[[int], None]:
		"""Create a callback method suitable for passing to :meth:`gui.runScriptModalDialog`.

		See class initialisation documentation for the meaning of parameters.

		:return: A callable which performs the appropriate update action based on the return code passed to it.
		"""

		def callback(res: int):
			match res:
				case wx.ID_OK:
					_executeUpdate(destPath)

				case wx.ID_CLOSE:
					finalDest = os.path.join(storeUpdatesDir, os.path.basename(destPath))
					try:
						# #9825: behavior of os.rename(s) has changed (see https://bugs.python.org/issue28356).
						# In Python 2, os.renames did rename files across drives, no longer allowed in Python 3 (error 17 (cannot move files across drives) is raised).
						# This is prominent when trying to postpone an update for portable copy of NVDA if this runs from a USB flash drive or another internal storage device.
						# Therefore use kernel32::MoveFileEx with copy allowed (0x2) flag set.
						# TODO: consider moving to shutil.move, which supports moves across filesystems.
						winKernel.moveFileEx(destPath, finalDest, winKernel.MOVEFILE_COPY_ALLOWED)
					except:  # noqa: E722
						log.debugWarning(
							f"Unable to rename the file from {destPath} to {finalDest}",
							exc_info=True,
						)
						gui.messageBox(
							# Translators: The message when a downloaded update file could not be preserved.
							_("Unable to postpone update."),
							# Translators: The title of the message when a downloaded update file could not be preserved.
							_("Error"),
							wx.OK | wx.ICON_ERROR,
						)
						finalDest = destPath
					state["pendingUpdateFile"] = finalDest
					state["pendingUpdateVersion"] = version
					state["pendingUpdateAPIVersion"] = apiVersion
					state["pendingUpdateBackCompatToAPIVersion"] = backCompatTo
					# Postponing an update indicates that the user is likely interested in getting a reminder.
					# Therefore, clear the dontRemindVersion.
					state["dontRemindVersion"] = None
					saveState()

				case _:
					log.error(f"Unexpected return code {res} from update dialog")

		return callback

	def onUpdateButton(self, evt):
		self.EndModal(wx.ID_OK)

	def onPostponeButton(self, evt):
		self.EndModal(wx.ID_CLOSE)


class UpdateDownloader(garbageHandler.TrackedObject):
	"""Download and start installation of an updated version of NVDA, presenting appropriate user interface.
	To use, call L{start} on an instance.
	"""

	def __init__(self, updateInfo: UpdateInfo):
		"""
		Constructor for the update downloader.
		:param updateInfo: An UpdateInfo object containing the metadata of the update,
		including version, URLs, and compatibility information.
		"""
		from addonAPIVersion import getAPIVersionTupleFromString

		self.updateInfo = updateInfo
		self.urls = updateInfo.launcherUrl.split(" ")
		self.version = updateInfo.version
		self.apiVersion = getAPIVersionTupleFromString(updateInfo.apiVersion)
		self.backCompatToAPIVersion = getAPIVersionTupleFromString(updateInfo.apiCompatTo)
		self.versionTuple = None
		self.fileHash = updateInfo.launcherHash
		self.destPath = _createEmptyTempFileForDeletingFile(prefix="nvda_update_", suffix=".exe")

	def start(self):
		"""Start the download."""
		self._shouldCancel = False
		# Use a timer because timers aren't re-entrant.
		self._guiExecTimer = gui.NonReEntrantTimer(self._guiExecNotify)
		gui.mainFrame.prePopup()
		self._progressDialog = wx.ProgressDialog(
			# Translators: The title of the dialog displayed while downloading an NVDA update.
			_("Downloading Update"),
			# Translators: The progress message indicating that a connection is being established.
			_("Connecting"),
			# PD_AUTO_HIDE is required because ProgressDialog.Update blocks at 100%
			# and waits for the user to press the Close button.
			style=wx.PD_CAN_ABORT | wx.PD_ELAPSED_TIME | wx.PD_REMAINING_TIME | wx.PD_AUTO_HIDE,
			parent=gui.mainFrame,
		)
		self._progressDialog.CentreOnScreen()
		self._progressDialog.Raise()
		t = threading.Thread(
			name=f"{self.__class__.__module__}.{self.start.__qualname__}",
			target=self._bg,
			daemon=True,
		)
		t.start()

	def _guiExec(self, func, *args):
		self._guiExecFunc = func
		self._guiExecArgs = args
		if not self._guiExecTimer.IsRunning():
			# #6127: Timers must be manipulated from the main thread.
			wx.CallAfter(self._guiExecTimer.Start, 50, True)

	def _guiExecNotify(self):
		self._guiExecFunc(*self._guiExecArgs)

	def _bg(self):
		success = False
		for url in self.urls:
			try:
				self._download(url)
			except:  # noqa: E722
				log.error("Error downloading %s" % url, exc_info=True)
			else:  # Successfully downloaded or canceled
				if not self._shouldCancel:
					success = True
				break
		else:
			# None of the URLs succeeded.
			self._guiExec(self._error)
			return
		if not success:
			try:
				os.remove(self.destPath)
			except OSError:
				pass
			return
		self._guiExec(self._downloadSuccess)

	def _download(self, url):
		# #2352: Some security scanners such as Eset NOD32 HTTP Scanner
		# cause huge read delays while downloading.
		# Therefore, set a higher timeout.
		# The NVDA exe is about 35 MB.
		# The average download speed in the world is 0.5 MB/s
		# in some developing countries with the slowest internet.
		# This yields an expected download time of 10min on slower networks.
		UPDATE_DOWNLOAD_TIMEOUT = 60 * 30  # 30 min
		remote = urllib.request.urlopen(url, timeout=UPDATE_DOWNLOAD_TIMEOUT)
		if remote.code != 200:
			raise RuntimeError("Download failed with code %d" % remote.code)
		size = int(remote.headers["content-length"])
		with open(self.destPath, "wb") as local:
			if self.fileHash:
				hasher = hashlib.sha1()
			self._guiExec(self._downloadReport, 0, size)
			read = 0
			chunk = DOWNLOAD_BLOCK_SIZE
			while True:
				if self._shouldCancel:
					return
				if size - read < chunk:
					chunk = size - read
				block = remote.read(chunk)
				if not block:
					break
				read += len(block)
				if self._shouldCancel:
					return
				local.write(block)
				if self.fileHash:
					hasher.update(block)
				self._guiExec(self._downloadReport, read, size)
			if read < size:
				raise RuntimeError("Content too short")
			if self.fileHash and hasher.hexdigest() != self.fileHash:
				raise RuntimeError("Content has incorrect file hash")
		self._guiExec(self._downloadReport, read, size)

	def _downloadReport(self, read, size):
		if self._shouldCancel:
			return
		percent = int(float(read) / size * 100)
		# Translators: The progress message indicating that a download is in progress.
		cont, skip = self._progressDialog.Update(percent, _("Downloading"))
		if not cont:
			self._shouldCancel = True
			self._stopped()

	def _stopped(self):
		self._guiExecTimer = None
		self._guiExecFunc = None
		self._guiExecArgs = None
		self._progressDialog.Hide()
		self._progressDialog.Destroy()
		self._progressDialog = None
		# Not sure why, but this doesn't work if we call it directly here.
		wx.CallLater(50, gui.mainFrame.postPopup)

	def _error(self):
		self._stopped()
		gui.messageBox(
			# Translators: A message indicating that an error occurred while downloading an update to NVDA.
			_("Error downloading update."),
			_("Error"),
			wx.OK | wx.ICON_ERROR,
		)

	def _downloadSuccess(self):
		self._stopped()
		askInstallDialog = UpdateAskInstallDialog(
			parent=gui.mainFrame,
			destPath=self.destPath,
			version=self.version,
			apiVersion=self.apiVersion,
			backCompatTo=self.backCompatToAPIVersion,
		)
		gui.runScriptModalDialog(
			askInstallDialog,
			callback=askInstallDialog.callback,
		)


class DonateRequestDialog(wx.Dialog):
	MESSAGE = _(
		# Translators: The message requesting donations from users.
		"We need your help in order to continue to improve NVDA.\n"
		"This project relies primarily on donations and grants. By donating, you are helping to fund full time development.\n"
		"If even $10 is donated for every download, we will be able to cover all of the ongoing costs of the project.\n"
		"All donations are received by NV Access, the non-profit organisation which develops NVDA.\n"
		"Thank you for your support.",
	)

	def __init__(self, parent, continueFunc):
		# Translators: The title of the dialog requesting donations from users.
		super(DonateRequestDialog, self).__init__(parent, title=_("Please Donate"))
		self._continue = continueFunc

		mainSizer = wx.BoxSizer(wx.VERTICAL)
		item = wx.StaticText(self, label=self.MESSAGE)
		mainSizer.Add(item, border=20, flag=wx.LEFT | wx.RIGHT | wx.TOP)
		sizer = wx.BoxSizer(wx.HORIZONTAL)
		# Translators: The label of the button to donate
		# in the "Please Donate" dialog.
		item = self.donateButton = wx.Button(self, label=_("&Donate"))
		item.Bind(wx.EVT_BUTTON, self.onDonate)
		sizer.Add(item)
		# Translators: The label of the button to decline donation
		# in the "Please Donate" dialog.
		item = wx.Button(self, wx.ID_CLOSE, label=_("&Not now"))
		item.Bind(wx.EVT_BUTTON, lambda evt: self.Close())
		sizer.Add(item)
		self.Bind(wx.EVT_CLOSE, self.onClose)
		self.EscapeId = wx.ID_CLOSE
		mainSizer.Add(sizer, flag=wx.TOP | wx.BOTTOM | wx.ALIGN_CENTER_HORIZONTAL, border=20)

		self.Sizer = mainSizer
		mainSizer.Fit(self)
		self.CentreOnScreen()
		self.Show()

	def onDonate(self, evt):
		os.startfile(gui.DONATE_URL)
		# Translators: The label of a button to indicate that the user is finished donating
		# in the "Please Donate" dialog.
		self.donateButton.Label = _("&Done")
		self.donateButton.Bind(wx.EVT_BUTTON, lambda evt: self.Close())

	def onClose(self, evt):
		self.Hide()
		self._continue()
		self.Destroy()


def saveState():
	try:
		# #9038: Python 3 requires binary format when working with pickles.
		with open(WritePaths.updateCheckStateFile, "wb") as f:
			pickle.dump(state, f, protocol=0)
	except:  # noqa: E722
		log.debugWarning("Error saving state", exc_info=True)


def initialize():
	global state, autoChecker
	try:
		# #9038: Python 3 requires binary format when working with pickles.
		with open(WritePaths.updateCheckStateFile, "rb") as f:
			state = pickle.load(f)
	except:  # noqa: E722
		log.debugWarning("Couldn't retrieve update state", exc_info=True)
		state = None

	if state is None:
		# Defaults.
		state = {
			"lastCheck": 0,
			"dontRemindVersion": None,
		}
		_setStateToNone(state)

	if "id" not in state:
		# ID was introduced in 2024.3
		state["id"] = uuid4().hex

	# check the pending version against the current version
	# and make sure that pendingUpdateFile and pendingUpdateVersion are part of the state dictionary.
	if "pendingUpdateVersion" not in state or state["pendingUpdateVersion"] == versionInfo.version:
		_setStateToNone(state)
	# remove all update files except the one that is currently pending (if any)
	try:
		for fileName in os.listdir(storeUpdatesDir):
			f = os.path.join(storeUpdatesDir, fileName)
			if f != state["pendingUpdateFile"]:
				os.remove(f)
				log.debug("Update file %s removed" % f)
	except OSError:
		log.warning("Unable to remove old update file %s" % f, exc_info=True)

	if not globalVars.appArgs.launcher and (
		config.conf["update"]["autoCheck"]
		or (config.conf["update"]["startupNotification"] and isPendingUpdate())
	):
		autoChecker = AutoUpdateChecker()


def terminate():
	global state, autoChecker
	state = None
	if autoChecker:
		autoChecker.terminate()
		autoChecker = None

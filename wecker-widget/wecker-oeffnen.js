// Variables used by Scriptable.
// These must be at the very top of the file. Do not edit.
// icon-color: orange; icon-glyph: bell;

// Wecker öffnen for Scriptable
// Opens the Clock app on its Alarms tab.
// Run it from the Scriptable app, a home screen bookmark, or a widget set to
// "When Interacting: Run Script".
//
// clock-alarm:// is not documented by Apple and may not work on every iOS version.
// If it does nothing, create a Shortcut with the single action "Open App → Clock"
// and put its name in FALLBACK_SHORTCUT. That always opens the Clock app, but on
// whichever tab was open last, not necessarily Alarms.

// ===== Settings =====
const ALARMS_URL = "clock-alarm://"
const FALLBACK_SHORTCUT = "" // e.g. "Uhr öffnen"; "" = use ALARMS_URL

// ===== Main =====
const url = FALLBACK_SHORTCUT
  ? `shortcuts://run-shortcut?name=${encodeURIComponent(FALLBACK_SHORTCUT)}`
  : ALARMS_URL
Safari.open(url)
Script.complete()

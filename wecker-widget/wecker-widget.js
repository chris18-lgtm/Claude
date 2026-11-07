// Variables used by Scriptable.
// These must be at the very top of the file. Do not edit.
// icon-color: orange; icon-glyph: clock;

// Wecker-Widget for Scriptable
// Shows how long until your next iPhone alarm rings.
//
// Scriptable cannot read the Clock app's alarms itself, so a Shortcut feeds them in:
// one line per enabled alarm, formatted "HH:mm|<repeat field>|<label>".
//   - Run by the Shortcut: saves the alarms and returns a text for a notification.
//   - Run as a widget:     shows a live countdown to the next alarm.
//   - Run in the app:      shows a preview of the small widget.

// ===== Settings =====
const TEXT = {
  alarm: "Wecker",
  ringsIn: "Klingelt in",
  in: "in",
  none: "Kein Wecker aktiv",
  setup: "Uhr-App einmal öffnen und schließen",
  lessThanMinute: "weniger als 1 Min",
  day: "Tag",
  days: "Tagen",
  hours: "Std",
  minutes: "Min",
  skipped: "Wecker nicht lesbar",
  today: "Heute",
  tomorrow: "Morgen",
  later: "Danach",
  noMore: "Keine weiteren",
}
const LOCALE = "de_DE"
const NOTIFY_WHEN_NONE = false // true: notification "Kein Wecker aktiv" after the last alarm is turned off
// A one-time alarm stays enabled while it rings or is snoozed (9 min per snooze).
// If the Shortcut runs within this many minutes after its time, it counts as today's
// (already rung) alarm instead of tomorrow's.
const SNOOZE_WINDOW_MIN = 20
// Tapping the widget opens the Clock app. clock-alarm:// goes straight to the Alarms tab,
// but Apple does not document it. If a tap does not open the Clock app, create a Shortcut
// with the single action "Open App → Clock" and put its name here; a tap then runs it.
const CLOCK_SHORTCUT = "Uhr öffnen" // clock-alarm:// did not open the Clock app on the iPhone this was tested on; "" = try it
const TAP_URL = CLOCK_SHORTCUT
  ? `shortcuts://run-shortcut?name=${encodeURIComponent(CLOCK_SHORTCUT)}`
  : "clock-alarm://"
const URGENT_HOURS = 6 // the countdown turns red when the alarm is closer than this; 0 = off

// ===== Core logic (no Scriptable APIs, testable on its own) =====
const DAY_TOKENS = {
  mo: 1, montag: 1, mon: 1, monday: 1,
  di: 2, dienstag: 2, tu: 2, tue: 2, tues: 2, tuesday: 2,
  mi: 3, mittwoch: 3, we: 3, wed: 3, wednesday: 3,
  do: 4, donnerstag: 4, th: 4, thu: 4, thur: 4, thurs: 4, thursday: 4,
  fr: 5, freitag: 5, fri: 5, friday: 5,
  sa: 6, samstag: 6, sat: 6, saturday: 6,
  so: 0, sonntag: 0, su: 0, sun: 0, sunday: 0,
}
const ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]

// Returns { once, days } or null if the repeat text is not understood.
// days uses JavaScript numbering: 0 = Sunday ... 6 = Saturday.
// Day names are matched as whole words, so "Saturday" never counts as "Tu".
function parseRepeat(raw) {
  const s = String(raw ?? "").toLowerCase().trim()
  if (s === "" || /^(nie|never|keine|none)$/.test(s)) return { once: true, days: [] }
  if (/täglich|taeglich|jeden tag|every ?day|daily/.test(s)) return { once: false, days: ALL_DAYS }
  if (/wochentags|werktags|weekdays?/.test(s)) return { once: false, days: [1, 2, 3, 4, 5] }
  if (/wochenende|weekends?/.test(s)) return { once: false, days: [0, 6] }
  const days = new Set()
  for (const token of s.split(/[^a-zäöüß]+/)) {
    if (Object.prototype.hasOwnProperty.call(DAY_TOKENS, token)) days.add(DAY_TOKENS[token])
  }
  return days.size ? { once: false, days: [...days].sort((a, b) => a - b) } : null
}

// "06:30|Mo, Di, Mi, Do, Fr|Arbeit" -> { h, m, once, days, label } or null
function parseLine(line) {
  const parts = String(line).split("|")
  const time = /(\d{1,2})[:.](\d{2})\s*(am|pm)?/i.exec(parts[0] || "")
  if (!time) return null
  let h = Number(time[1])
  const m = Number(time[2])
  const ampm = (time[3] || "").toLowerCase()
  if (ampm === "pm" && h < 12) h += 12
  if (ampm === "am" && h === 12) h = 0
  if (h > 23 || m > 59) return null
  const repeat = parseRepeat(parts[1])
  if (!repeat) return null
  return { h, m, once: repeat.once, days: repeat.days, label: parts.slice(2).join("|").trim() }
}

// Next ring time strictly after `now`, or null.
// A one-time alarm keeps the date it had when the Shortcut saw it (`at`),
// so after it has rung the widget no longer counts down to it.
function nextOccurrence(alarm, now) {
  if (alarm.once && alarm.at) {
    const at = new Date(alarm.at)
    return at > now ? at : null
  }
  const days = alarm.once ? ALL_DAYS : alarm.days
  for (let i = 0; i <= 7; i++) {
    // Local wall-clock time: daylight saving changes are handled by Date itself.
    const c = new Date(now.getFullYear(), now.getMonth(), now.getDate() + i, alarm.h, alarm.m, 0, 0)
    if (c > now && days.includes(c.getDay())) return c
  }
  return null
}

// All rings of one alarm in the coming two weeks, strictly after `now`.
// Two weeks, so the large widget can fill its list even with only weekday alarms.
function occurrences(alarm, now) {
  if (alarm.once) {
    const date = nextOccurrence(alarm, now)
    return date ? [date] : []
  }
  const out = []
  for (let i = 0; i <= 14; i++) {
    const c = new Date(now.getFullYear(), now.getMonth(), now.getDate() + i, alarm.h, alarm.m, 0, 0)
    if (c > now && alarm.days.includes(c.getDay())) out.push(c)
  }
  return out
}

// The next `count` rings across all alarms, earliest first: [{ date, alarm }]
function upcomingAlarms(alarms, now, count) {
  return alarms
    .flatMap((alarm) => occurrences(alarm, now).map((date) => ({ date, alarm })))
    .sort((a, b) => a.date - b.date)
    .slice(0, count)
}

function nextAlarm(alarms, now) {
  return upcomingAlarms(alarms, now, 1)[0] ?? null
}

const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate())

// round(): a day with a daylight saving change is 23 or 25 hours long
function calendarDaysBetween(from, to) {
  return Math.round((startOfDay(to) - startOfDay(from)) / 86400000)
}

// "Heute" / "Morgen" by calendar day, or null for later days.
function relativeDayName(date, now) {
  const diff = calendarDaysBetween(now, date)
  if (diff === 0) return TEXT.today
  if (diff === 1) return TEXT.tomorrow
  return null
}

function isUrgent(date, now) {
  return URGENT_HOURS > 0 && date - now < URGENT_HOURS * 3600 * 1000
}

// iOS names an alarm without a label "Alarm" (German: "Wecker"); showing that adds nothing.
function displayLabel(label) {
  return /^(alarm|wecker)$/i.test(String(label ?? "").trim()) ? "" : String(label ?? "").trim()
}

// Scriptable widgets have no timeline: one rendering stays until iOS refreshes it.
// So ask for a refresh at the first moment the content would change: midnight
// ("Morgen" becomes "Heute"), the switch to urgent, or the ring itself.
function nextRefresh(next, now) {
  const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1)
  const candidates = [midnight]
  if (next) {
    candidates.push(next.date)
    if (URGENT_HOURS > 0) candidates.push(new Date(next.date - URGENT_HOURS * 3600 * 1000))
  } else {
    candidates.push(new Date(now.getTime() + 3600 * 1000))
  }
  return candidates.filter((d) => d > now).reduce((a, b) => (b < a ? b : a))
}

// The date a one-time alarm rings, as seen by the Shortcut at `now`.
function oneTimeRingDate(alarm, now) {
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate(), alarm.h, alarm.m, 0, 0)
  const sinceMs = now - today
  if (sinceMs >= 0 && sinceMs <= SNOOZE_WINDOW_MIN * 60 * 1000) return today
  return nextOccurrence({ ...alarm, at: undefined }, now)
}

// "widget" | "shortcut" | "preview". Only "shortcut" overwrites the saved alarms,
// and an empty input then means "all alarms are off". The share sheet and
// notifications never get there, so they cannot wipe the saved alarms.
function runMode(config, args) {
  if (config.runsInWidget) return "widget"
  if (config.runsInActionExtension || config.runsInNotification) return "preview"
  const hasParameter = args.shortcutParameter !== null && args.shortcutParameter !== undefined
  if (hasParameter || config.runsWithSiri || !config.runsInApp) return "shortcut"
  return "preview"
}

// What the widget shows when there is no upcoming alarm.
function emptyMessage(state) {
  if (!state) return TEXT.setup
  if (state.skipped) return `${state.skipped} ${TEXT.skipped}`
  return TEXT.none
}

// 8 h 30 min -> "8 Std 30 Min"; from one day on, minutes are dropped: "2 Tagen 8 Std"
function formatRemaining(ms) {
  const total = Math.floor(ms / 60000)
  if (total < 1) return TEXT.lessThanMinute
  const d = Math.floor(total / 1440)
  const h = Math.floor((total % 1440) / 60)
  const m = total % 60
  const parts = []
  if (d) parts.push(`${d} ${d === 1 ? TEXT.day : TEXT.days}`)
  if (h) parts.push(`${h} ${TEXT.hours}`)
  if (m && !d) parts.push(`${m} ${TEXT.minutes}`)
  return parts.join(" ")
}
// ===== End core logic =====

// ===== Storage =====
const fm = FileManager.local()
const STORE = fm.joinPath(fm.documentsDirectory(), "wecker-widget.json")

function loadState() {
  if (!fm.fileExists(STORE)) return null
  try {
    return JSON.parse(fm.readString(STORE))
  } catch (e) {
    return null
  }
}

function saveState(state) {
  fm.writeString(STORE, JSON.stringify(state))
}

// ===== Formatting =====
function formatDate(date, pattern) {
  const df = new DateFormatter()
  df.locale = LOCALE
  df.dateFormat = pattern
  return df.string(date)
}

// "Heute", "Morgen", "Mo."; from 7 days on the weekday repeats, so the date is added: "Mo. 16.11."
function formatDay(date, now) {
  return relativeDayName(date, now)
    ?? formatDate(date, calendarDaysBetween(now, date) >= 7 ? "EE dd.MM." : "EE")
}

// "Heute 06:30", "Morgen 06:30", "Mo. 06:30", "Mo. 16.11. 06:30"
function formatWhen(date, now) {
  return `${formatDay(date, now)} ${formatDate(date, "HH:mm")}`
}

// ===== Run from the Shortcut =====
function runFromShortcut(now) {
  let input = args.shortcutParameter
  if (Array.isArray(input)) input = input.join("\n")
  if (input == null || input === "") input = (args.plainTexts || []).join("\n")
  const lines = String(input ?? "").split(/\r?\n/).map((l) => l.trim()).filter(Boolean)

  const alarms = []
  let skipped = 0
  for (const line of lines) {
    const alarm = parseLine(line)
    if (!alarm) {
      skipped++
      continue
    }
    if (alarm.once) alarm.at = oneTimeRingDate(alarm, now).toISOString()
    alarms.push(alarm)
  }
  saveState({ savedAt: now.toISOString(), alarms, skipped })

  const next = nextAlarm(alarms, now)
  let message = ""
  if (next) {
    const when = formatWhen(next.date, now)
    const label = displayLabel(next.alarm.label) ? `, ${displayLabel(next.alarm.label)}` : ""
    message = `⏰ ${TEXT.ringsIn} ${formatRemaining(next.date - now)} (${when}${label})`
  } else if (NOTIFY_WHEN_NONE) {
    message = TEXT.none
  }
  if (skipped) message += `${message ? "\n" : ""}${skipped} ${TEXT.skipped}`
  Script.setShortcutOutput(message)
}

// ===== Widget =====
// Styled after Apple's Clock and Sleep widgets: system background, one big number,
// at most two small lines, orange for alarms, 16 pt margins.
const ACCENT = new Color("#FF9F0A") // the orange of the Clock app's alarms
const RED = Color.dynamic(new Color("#FF3B30"), new Color("#FF453A")) // iOS system red
const INK = Color.dynamic(new Color("#000000"), new Color("#FFFFFF")) // label
const QUIET = Color.dynamic(new Color("#6C6C70"), new Color("#8E8E93")) // secondary label
const PAPER = Color.dynamic(new Color("#FFFFFF"), new Color("#1C1C1E")) // widget background

function alarmIcon(size, color) {
  const symbol = SFSymbol.named("alarm.fill")
  symbol.applyFont(Font.semiboldSystemFont(size))
  return { image: symbol.image, color }
}

function addIcon(stack, size, color) {
  const icon = alarmIcon(size, color)
  const img = stack.addImage(icon.image)
  img.imageSize = new Size(size, size)
  img.tintColor = icon.color
  return img
}

// A row with text followed by a flexible spacer, so it sits left in every widget size.
function addRow(stack) {
  const row = stack.addStack()
  row.centerAlignContent()
  return row
}

function addText(stack, text, font, color, lines = 1) {
  const t = stack.addText(text)
  t.font = font
  t.textColor = color
  t.lineLimit = lines
  return t
}

// "in 7 Std., 30 Min." – the countdown iOS keeps updating on its own.
function addCountdownLine(stack, date, size, color) {
  const row = addRow(stack)
  addText(row, TEXT.in, Font.systemFont(size), color)
  row.addSpacer(size * 0.3) // a fixed gap; a trailing space in the text could be trimmed
  const d = row.addDate(date)
  d.applyRelativeStyle()
  d.font = Font.systemFont(size)
  d.textColor = color
  d.lineLimit = 1
  d.minimumScaleFactor = 0.7
  row.addSpacer()
}

function buildWidget(state, now) {
  const family = config.widgetFamily || "small"
  const w = new ListWidget()
  if (TAP_URL) w.url = TAP_URL
  const next = state ? nextAlarm(state.alarms || [], now) : null
  // After the ring time iOS counts the relative date *up* ("3 Min" again), so the
  // ring time is one of the refresh moments. iOS treats this as a request, not a promise.
  w.refreshAfterDate = nextRefresh(next, now)
  const emptyText = emptyMessage(state)
  const label = next ? displayLabel(next.alarm.label) : ""

  if (family === "accessoryInline") {
    w.addText(`⏰ ${next ? formatWhen(next.date, now) : emptyText}`)
    return w
  }

  if (family === "accessoryCircular") {
    w.addAccessoryWidgetBackground = true
    const img = addIcon(w, 13, Color.white())
    img.centerAlignImage()
    const t = w.addText(next ? formatDate(next.date, "HH:mm") : "–")
    t.font = Font.semiboldRoundedSystemFont(15)
    t.minimumScaleFactor = 0.6
    t.centerAlignText()
    return w
  }

  if (family === "accessoryRectangular") {
    // Lock Screen: iOS tints everything itself, so only weight and size create hierarchy.
    if (!next) {
      const row = addRow(w)
      addIcon(row, 12, Color.white())
      row.addSpacer(4)
      addText(row, TEXT.alarm, Font.semiboldSystemFont(13), Color.white())
      row.addSpacer()
      const t = addText(w, emptyText, Font.systemFont(13), Color.white(), 2)
      t.minimumScaleFactor = 0.7
      return w
    }
    const row = addRow(w)
    addIcon(row, 12, Color.white())
    row.addSpacer(4)
    addText(row, label || formatDay(next.date, now), Font.semiboldSystemFont(13), Color.white())
    row.addSpacer()
    addText(w, label ? formatWhen(next.date, now) : formatDate(next.date, "HH:mm"),
      Font.semiboldRoundedSystemFont(22), Color.white()).minimumScaleFactor = 0.6
    addCountdownLine(w, next.date, 12, Color.white())
    return w
  }

  // Home screen: small, medium, large
  w.backgroundColor = PAPER
  w.setPadding(16, 16, 16, 16)

  if (!next) {
    const head = addRow(w)
    addIcon(head, 15, ACCENT)
    head.addSpacer(5)
    addText(head, TEXT.alarm, Font.semiboldSystemFont(15), ACCENT)
    head.addSpacer()
    w.addSpacer()
    const t = addText(w, emptyText, Font.semiboldSystemFont(17), INK, 3)
    t.minimumScaleFactor = 0.7
    return w
  }

  const urgent = isUrgent(next.date, now)
  const big = { small: 46, medium: 46, large: 64 }[family] || 46

  // Top: orange alarm symbol and the day, like the header of Apple's Sleep widget.
  const head = addRow(w)
  addIcon(head, 15, ACCENT)
  head.addSpacer(5)
  addText(head, formatDay(next.date, now), Font.semiboldSystemFont(15), ACCENT)
  head.addSpacer()
  w.addSpacer()

  const main = family === "medium" ? w.addStack() : w
  if (family === "medium") main.bottomAlignContent()
  const block = family === "medium" ? main.addStack() : w
  if (family === "medium") block.layoutVertically()

  // The one big number: the ring time. It only changes when the alarm does.
  const time = addText(block, formatDate(next.date, "HH:mm"), Font.semiboldRoundedSystemFont(big), INK)
  time.minimumScaleFactor = 0.6
  if (label) addText(block, label, Font.mediumSystemFont(14), INK)
  addCountdownLine(block, next.date, 13, urgent ? RED : QUIET)
  // Otherwise an unreadable alarm would only ever be mentioned in one notification.
  if (state.skipped) addText(block, `⚠︎ ${state.skipped} ${TEXT.skipped}`, Font.systemFont(11), QUIET)

  if (family === "medium") {
    main.addSpacer(12)
    main.addSpacer()
    const list = main.addStack()
    list.layoutVertically()
    list.size = new Size(140, 0) // fixed width, height fits the content
    addLater(list, upcomingAlarms(state.alarms, now, 4).slice(1), now, 13)
  }
  if (family === "large") {
    w.addSpacer(20)
    addLater(w, upcomingAlarms(state.alarms, now, 8).slice(1), now, 15)
  }
  return w
}

// "Danach" and one row per following ring: "Morgen 06:30  Zahnarzt"
function addLater(stack, later, now, size) {
  addText(stack, TEXT.later, Font.semiboldSystemFont(size - 1), QUIET)
  stack.addSpacer(4)
  if (!later.length) {
    addText(stack, TEXT.noMore, Font.systemFont(size), QUIET)
    return
  }
  for (const { date, alarm } of later) {
    const row = addRow(stack)
    addText(row, formatDay(date, now), Font.systemFont(size), QUIET)
    row.addSpacer(6)
    addText(row, formatDate(date, "HH:mm"), Font.semiboldRoundedSystemFont(size), INK)
    const name = displayLabel(alarm.label)
    if (name) {
      row.addSpacer(6)
      addText(row, name, Font.systemFont(size), QUIET)
    }
    row.addSpacer()
    stack.addSpacer(4)
  }
}

// ===== Main =====
const now = new Date()
const mode = runMode(config, args)

if (mode === "widget") {
  Script.setWidget(buildWidget(loadState(), now))
} else if (mode === "shortcut") {
  runFromShortcut(now)
} else {
  await buildWidget(loadState(), now).presentSmall()
}
Script.complete()

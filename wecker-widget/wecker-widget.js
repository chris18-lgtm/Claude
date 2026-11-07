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
  header: "Nächster Wecker",
  ringsIn: "Klingelt in",
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
const URGENT_HOURS = 6 // home-screen ring time turns red when the alarm is closer than this; 0 = off
const TIME_GRADIENT = true // home-screen background follows the time of day; false = plain white/black

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

// Background colours by time of day: [start hour, phase]
const PHASES = [[5, "morning"], [9, "day"], [17, "evening"], [22, "night"]]

function dayPhase(now) {
  let phase = PHASES[PHASES.length - 1][1] // before the first start: still night
  for (const [start, name] of PHASES) if (now.getHours() >= start) phase = name
  return phase
}

function nextPhaseChange(now) {
  for (const [start] of PHASES) {
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate(), start)
    if (d > now) return d
  }
  return new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1, PHASES[0][0])
}

// Scriptable widgets have no timeline: one rendering stays until iOS refreshes it.
// So ask for a refresh at the first moment the content would change: midnight
// ("Morgen" becomes "Heute"), the next background colour, the switch to urgent,
// or the ring itself.
function nextRefresh(next, now) {
  const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1)
  const candidates = [midnight]
  if (TIME_GRADIENT) candidates.push(nextPhaseChange(now))
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

// "Heute 06:30", "Morgen 06:30", "Mo. 06:30"; from 7 days on the weekday repeats,
// so the date is added: "Mo. 16.11. 06:30"
function formatWhen(date, now) {
  const day = relativeDayName(date, now)
  if (day) return `${day} ${formatDate(date, "HH:mm")}`
  return formatDate(date, calendarDaysBetween(now, date) >= 7 ? "EE dd.MM. HH:mm" : "EE HH:mm")
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
    const label = next.alarm.label ? `, ${next.alarm.label}` : ""
    message = `⏰ ${TEXT.ringsIn} ${formatRemaining(next.date - now)} (${when}${label})`
  } else if (NOTIFY_WHEN_NONE) {
    message = TEXT.none
  }
  if (skipped) message += `${message ? "\n" : ""}${skipped} ${TEXT.skipped}`
  Script.setShortcutOutput(message)
}

// ===== Widget =====
const ACCENT = new Color("#FF9F0A") // the orange of the Clock app's alarms
const URGENT = new Color("#FF453A") // iOS red, used when the alarm is closer than URGENT_HOURS
const INK = Color.dynamic(new Color("#1C1C1E"), new Color("#FFFFFF"))
const QUIET = Color.dynamic(new Color("#6E6E73"), new Color("#AEAEB2"))
const PAPER = Color.dynamic(new Color("#FFFFFF"), new Color("#1C1C1E"))

// Time-of-day backgrounds, top left to bottom right. White and 80 % white text stay
// readable on both ends (contrast about 4.5:1 or better).
const GRADIENTS = {
  night: { colors: ["#0B1026", "#2A3470"], accent: "#FFC266" },
  morning: { colors: ["#3B2F7A", "#99402F"], accent: "#FFE2A8" },
  day: { colors: ["#0F4C9E", "#175FA0"], accent: "#FFE08A" },
  evening: { colors: ["#3A1C71", "#8E3559"], accent: "#FFD0A1" },
}
// Red text would vanish on the evening and morning colours, so "soon" becomes a red pill.
const URGENT_PILL = new Color("#D92D20")

// Sets the home-screen background and returns the text colours: { ink, quiet, accent, pill }
function applyLook(w, now, urgent) {
  if (!TIME_GRADIENT) {
    w.backgroundColor = PAPER
    return { ink: INK, quiet: QUIET, accent: urgent ? URGENT : ACCENT, pill: null }
  }
  const look = GRADIENTS[dayPhase(now)]
  const bg = new LinearGradient()
  bg.colors = look.colors.map((hex) => new Color(hex))
  bg.locations = [0, 1]
  bg.startPoint = new Point(0, 0)
  bg.endPoint = new Point(1, 1)
  w.backgroundGradient = bg
  return {
    ink: Color.white(),
    quiet: new Color("#FFFFFF", 0.8),
    accent: new Color(look.accent),
    pill: urgent ? URGENT_PILL : null,
  }
}

function alarmIcon(size, color) {
  const symbol = SFSymbol.named("alarm.fill")
  symbol.applyFont(Font.systemFont(size))
  return { image: symbol.image, color }
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

  if (family === "accessoryInline") {
    w.addText(`⏰ ${next ? formatWhen(next.date, now) : emptyText}`)
    return w
  }

  if (family === "accessoryCircular") {
    w.addAccessoryWidgetBackground = true
    const icon = alarmIcon(14, Color.white())
    const img = w.addImage(icon.image)
    img.imageSize = new Size(14, 14)
    img.tintColor = icon.color
    img.centerAlignImage()
    const t = w.addText(next ? formatDate(next.date, "HH:mm") : "–")
    t.font = Font.boldRoundedSystemFont(14)
    t.minimumScaleFactor = 0.6
    t.centerAlignText()
    return w
  }

  if (family === "accessoryRectangular") {
    const head = w.addText(`⏰ ${TEXT.header}`)
    head.font = Font.semiboldSystemFont(12)
    if (next) {
      const d = w.addDate(next.date)
      d.applyRelativeStyle()
      d.font = Font.boldRoundedSystemFont(17)
      d.minimumScaleFactor = 0.7
      d.lineLimit = 1
      const f = w.addText(formatWhen(next.date, now))
      f.font = Font.systemFont(12)
      f.lineLimit = 1
    } else {
      const f = w.addText(emptyText)
      f.font = Font.systemFont(13)
      f.minimumScaleFactor = 0.7
    }
    return w
  }

  // Home screen: small, medium, large
  w.setPadding(14, 14, 14, 14)
  const look = applyLook(w, now, next && isUrgent(next.date, now))

  const top = w.addStack()
  top.centerAlignContent()
  const icon = alarmIcon(13, look.accent)
  const img = top.addImage(icon.image)
  img.imageSize = new Size(13, 13)
  img.tintColor = icon.color
  top.addSpacer(5)
  const head = top.addText(TEXT.header)
  head.font = Font.semiboldSystemFont(12)
  head.textColor = look.quiet
  top.addSpacer() // keep the header left-aligned in every size

  if (!next) {
    w.addSpacer()
    const msg = w.addText(emptyText)
    msg.font = Font.semiboldSystemFont(15)
    msg.textColor = look.ink
    msg.minimumScaleFactor = 0.7
    w.addSpacer()
    return w
  }

  if (family === "medium") {
    // Countdown on the left, the following rings on the right.
    w.addSpacer()
    const row = w.addStack()
    row.bottomAlignContent()
    const left = row.addStack()
    left.layoutVertically()
    addCountdown(left, next, state, now, look, 26)
    row.addSpacer(10) // minimum gap, so the countdown shrinks instead of touching the list
    row.addSpacer()
    const right = row.addStack()
    right.layoutVertically()
    right.size = new Size(135, 0) // fixed width, height fits the content
    addLater(right, upcomingAlarms(state.alarms, now, 4).slice(1), now, 12, look)
    return w
  }

  if (family === "large") {
    w.addSpacer(16)
    addCountdown(w, next, state, now, look, 34)
    w.addSpacer(20)
    addLater(w, upcomingAlarms(state.alarms, now, 9).slice(1), now, 14, look) // 8 rows fill the height
    w.addSpacer()
    return w
  }

  w.addSpacer()
  addCountdown(w, next, state, now, look, 24)
  return w
}

// "Klingelt in", the live countdown, ring time, label and a warning for unreadable alarms.
function addCountdown(stack, next, state, now, look, dateSize) {
  const lead = stack.addText(TEXT.ringsIn)
  lead.font = Font.systemFont(12)
  lead.textColor = look.quiet

  // The one big element: a countdown that iOS keeps updating on its own.
  const d = stack.addDate(next.date)
  d.applyRelativeStyle()
  d.font = Font.boldRoundedSystemFont(dateSize)
  d.textColor = look.ink
  d.minimumScaleFactor = 0.5
  d.lineLimit = 1 // shrink rather than break "8 Std., 45 Min." after the number

  stack.addSpacer(4)
  // Less than URGENT_HOURS left: a red pill on the gradient, red text on the plain background.
  let whenStack = stack
  if (look.pill) {
    const row = stack.addStack() // the trailing spacer keeps the pill left and text-wide
    whenStack = row.addStack()
    row.addSpacer()
    whenStack.backgroundColor = look.pill
    whenStack.cornerRadius = 6
    whenStack.setPadding(2, 6, 2, 6)
  }
  const when = whenStack.addText(formatWhen(next.date, now))
  when.font = Font.semiboldRoundedSystemFont(13)
  when.textColor = look.pill ? Color.white() : look.accent
  when.lineLimit = 1
  if (next.alarm.label) {
    const label = stack.addText(next.alarm.label)
    label.font = Font.systemFont(11)
    label.textColor = look.quiet
    label.lineLimit = 1
  }
  // Otherwise an unreadable alarm would only ever be mentioned in one notification.
  if (state.skipped) {
    const warn = stack.addText(`⚠︎ ${state.skipped} ${TEXT.skipped}`)
    warn.font = Font.systemFont(10)
    warn.textColor = look.quiet
    warn.lineLimit = 1
  }
}

// "DANACH" and one row per following ring: "Morgen 06:30  Arbeit"
function addLater(stack, later, now, size, look) {
  const head = stack.addText(TEXT.later.toUpperCase())
  head.font = Font.semiboldSystemFont(10)
  head.textColor = look.quiet
  stack.addSpacer(4)
  if (!later.length) {
    const t = stack.addText(TEXT.noMore)
    t.font = Font.systemFont(size)
    t.textColor = look.quiet
    return
  }
  for (const { date, alarm } of later) {
    const row = stack.addStack()
    row.centerAlignContent()
    const t = row.addText(formatWhen(date, now))
    t.font = Font.semiboldRoundedSystemFont(size)
    t.textColor = look.ink
    t.lineLimit = 1
    if (alarm.label) {
      row.addSpacer(6)
      const l = row.addText(alarm.label)
      l.font = Font.systemFont(size)
      l.textColor = look.quiet
      l.lineLimit = 1
    }
    stack.addSpacer(3)
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

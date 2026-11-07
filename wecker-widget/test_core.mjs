// Tests for the Scriptable-free part of wecker-widget.js.
// Run: node --test wecker-widget/test_core.mjs
import { test } from "node:test"
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"

const src = readFileSync(new URL("./wecker-widget.js", import.meta.url), "utf8")
const start = src.indexOf("// ===== Settings =====")
const end = src.indexOf("// ===== End core logic =====")
assert.ok(start >= 0 && end > start, "section markers missing in wecker-widget.js")
const core = new Function(
  `${src.slice(start, end)}
  return { parseRepeat, parseLine, nextOccurrence, nextAlarm, formatRemaining,
           oneTimeRingDate, runMode, emptyMessage, relativeDayName, upcomingAlarms,
           isUrgent, nextRefresh, calendarDaysBetween, displayLabel, TEXT }`
)()

// Saturday 2026-11-07, local time
const at = (h, m, day = 7) => new Date(2026, 10, day, h, m, 0, 0)

test("parseRepeat understands German and English repeat texts", () => {
  assert.deepEqual(core.parseRepeat(""), { once: true, days: [] })
  assert.deepEqual(core.parseRepeat("Nie"), { once: true, days: [] })
  assert.deepEqual(core.parseRepeat("Jeden Tag").days, [0, 1, 2, 3, 4, 5, 6])
  assert.deepEqual(core.parseRepeat("Wochentags").days, [1, 2, 3, 4, 5])
  assert.deepEqual(core.parseRepeat("Am Wochenende").days, [0, 6])
  assert.deepEqual(core.parseRepeat("Mo., Mi., Fr.").days, [1, 3, 5])
  assert.deepEqual(core.parseRepeat("Saturday").days, [6])
  assert.equal(core.parseRepeat("Gelegentlich"), null)
})

test("parseLine reads 24 h and 12 h times and keeps '|' in labels", () => {
  assert.deepEqual(core.parseLine("06:30|Wochentags|Arbeit"),
    { h: 6, m: 30, once: false, days: [1, 2, 3, 4, 5], label: "Arbeit" })
  assert.equal(core.parseLine("6:30 PM||").h, 18)
  assert.equal(core.parseLine("12:05 AM||").h, 0)
  assert.equal(core.parseLine("07:00|Nie|A|B").label, "A|B")
  assert.equal(core.parseLine("25:00|Nie|"), null)
  assert.equal(core.parseLine("kaputt"), null)
})

test("nextOccurrence picks the next matching weekday", () => {
  const weekdays = core.parseLine("06:30|Wochentags|")
  assert.equal(core.nextOccurrence(weekdays, at(10, 0)).getTime(), at(6, 30, 9).getTime()) // Monday
  const daily = core.parseLine("23:00|Jeden Tag|")
  assert.equal(core.nextOccurrence(daily, at(10, 0)).getTime(), at(23, 0).getTime())
})

test("a one-time alarm with a stored date stops counting once it has rung", () => {
  const alarm = { ...core.parseLine("07:00|Nie|"), at: at(7, 0).toISOString() }
  assert.equal(core.nextOccurrence(alarm, at(6, 0)).getTime(), at(7, 0).getTime())
  assert.equal(core.nextOccurrence(alarm, at(7, 1)), null)
})

test("oneTimeRingDate: a one-time alarm that is ringing or snoozed is not moved to tomorrow", () => {
  const alarm = core.parseLine("07:00|Nie|")
  // Shortcut runs while the 07:00 alarm is snoozed (iOS keeps it enabled)
  assert.equal(core.oneTimeRingDate(alarm, at(7, 12)).getTime(), at(7, 0).getTime())
  // Far enough past the time: the user means tomorrow
  assert.equal(core.oneTimeRingDate(alarm, at(9, 0)).getTime(), at(7, 0, 8).getTime())
  // Still ahead today
  assert.equal(core.oneTimeRingDate(alarm, at(6, 0)).getTime(), at(7, 0).getTime())
})

test("nextAlarm returns the earliest alarm", () => {
  const alarms = [core.parseLine("08:00|Jeden Tag|spät"), core.parseLine("07:00|Jeden Tag|früh")]
  assert.equal(core.nextAlarm(alarms, at(6, 0)).alarm.label, "früh")
  assert.equal(core.nextAlarm([], at(6, 0)), null)
})

test("formatRemaining", () => {
  const min = 60000
  assert.equal(core.formatRemaining(30 * 1000), "weniger als 1 Min")
  assert.equal(core.formatRemaining((8 * 60 + 30) * min), "8 Std 30 Min")
  assert.equal(core.formatRemaining((2 * 1440 + 8 * 60 + 5) * min), "2 Tagen 8 Std")
  assert.equal(core.formatRemaining(1440 * min), "1 Tag")
})

test("runMode never saves from the share sheet or a notification", () => {
  const mode = (config, shortcutParameter = null) => core.runMode(config, { shortcutParameter })
  assert.equal(mode({ runsInWidget: true }), "widget")
  assert.equal(mode({ runsInApp: true }), "preview")
  assert.equal(mode({ runsInApp: true }, "07:00||"), "shortcut")
  assert.equal(mode({ runsInApp: false }), "shortcut") // Shortcut with no alarms left
  assert.equal(mode({ runsWithSiri: true }), "shortcut")
  assert.equal(mode({ runsInActionExtension: true }), "preview")
  assert.equal(mode({ runsInActionExtension: true }, "some shared text"), "preview")
  assert.equal(mode({ runsInNotification: true }), "preview")
})

test("emptyMessage does not claim 'no alarm' when alarms could not be read", () => {
  assert.equal(core.emptyMessage(null), core.TEXT.setup)
  assert.equal(core.emptyMessage({ alarms: [], skipped: 0 }), core.TEXT.none)
  assert.equal(core.emptyMessage({ alarms: [], skipped: 2 }), `2 ${core.TEXT.skipped}`)
})

test("relativeDayName says Heute/Morgen by calendar day, not by 24 h", () => {
  assert.equal(core.relativeDayName(at(23, 59), at(0, 1)), "Heute")
  assert.equal(core.relativeDayName(at(0, 5, 8), at(23, 55)), "Morgen") // 10 min away, still tomorrow
  assert.equal(core.relativeDayName(at(6, 30, 9), at(23, 0)), null)
})

test("upcomingAlarms lists the next rings across all alarms, repeats included", () => {
  const alarms = [core.parseLine("06:30|Wochentags|Arbeit"), core.parseLine("09:00|Nie|Ausschlafen")]
  const list = core.upcomingAlarms(alarms, at(10, 0), 4) // Saturday 10:00
  assert.deepEqual(list.map((x) => [x.date.getDate(), x.date.getHours(), x.alarm.label]), [
    [8, 9, "Ausschlafen"], [9, 6, "Arbeit"], [10, 6, "Arbeit"], [11, 6, "Arbeit"],
  ])
  assert.equal(core.upcomingAlarms([], at(10, 0), 3).length, 0)
  assert.equal(core.nextAlarm(alarms, at(10, 0)).alarm.label, "Ausschlafen")
})

test("isUrgent below URGENT_HOURS", () => {
  assert.equal(core.isUrgent(at(6, 0), at(0, 1)), true)
  assert.equal(core.isUrgent(at(6, 0), at(23, 0, 6)), false)
})

test("nextRefresh: the earliest moment the widget content changes", () => {
  const next = (date) => ({ date })
  // 22:00, alarm 06:30 tomorrow -> midnight comes first (label "Morgen" -> "Heute")
  assert.equal(core.nextRefresh(next(at(6, 30, 8)), at(22, 0)).getTime(), at(0, 0, 8).getTime())
  // 01:00, alarm 06:30 today -> already urgent, so the ring time
  assert.equal(core.nextRefresh(next(at(6, 30)), at(1, 0)).getTime(), at(6, 30).getTime())
  // 05:10, alarm 06:30 today -> the ring time
  assert.equal(core.nextRefresh(next(at(6, 30)), at(5, 10)).getTime(), at(6, 30).getTime())
  // 14:00, alarm 23:00 today -> urgent from 17:00
  assert.equal(core.nextRefresh(next(at(23, 0)), at(14, 0)).getTime(), at(17, 0).getTime())
  // no alarm -> one hour
  assert.equal(core.nextRefresh(null, at(14, 0)).getTime(), at(15, 0).getTime())
})

test("upcomingAlarms looks two weeks ahead", () => {
  const weekdays = [core.parseLine("06:30|Wochentags|Arbeit")]
  const list = core.upcomingAlarms(weekdays, at(10, 0), 9) // Saturday 7th
  assert.equal(list.length, 9)
  assert.equal(list[8].date.getDate(), 19) // Thursday of the week after next
})

test("calendarDaysBetween counts calendar days, also across a DST change", () => {
  assert.equal(core.calendarDaysBetween(at(23, 0), at(1, 0, 8)), 1)
  assert.equal(core.calendarDaysBetween(at(10, 0), at(6, 30, 16)), 9)
  // 2026-03-29: clocks go forward in Germany (a 23 h day if TZ observes it)
  assert.equal(core.calendarDaysBetween(new Date(2026, 2, 28, 12), new Date(2026, 2, 30, 12)), 2)
})

test("displayLabel hides iOS default names only", () => {
  assert.equal(core.displayLabel("Alarm"), "")
  assert.equal(core.displayLabel(" wecker "), "")
  assert.equal(core.displayLabel("Aufstehen"), "Aufstehen")
  assert.equal(core.displayLabel("Alarm Arbeit"), "Alarm Arbeit")
  assert.equal(core.displayLabel(undefined), "")
})

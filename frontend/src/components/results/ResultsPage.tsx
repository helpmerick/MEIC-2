import { useEffect, useState } from "react";
import { api } from "../../api";
import type { ReportPeriod } from "../../api";
import { resultsDayHref } from "../../router";
import type { DailyRow, EntryCard, ReportSummary } from "../../types";
import { CalendarHeatmap } from "./CalendarHeatmap";
import { EquityCurve } from "./EquityCurve";
import { HealthPanel } from "./HealthPanel";
import { RecordsBlock } from "./RecordsBlock";
import { CsvButton, dollars, pct, PaperBanner, plainDollars, signClass, TrustBadge } from "./shared";
import { SlippagePanels } from "./SlippagePanels";
import { TargetingPanel } from "./TargetingPanel";
import { TodayHero } from "./TodayHero";
import { Waterfall } from "./Waterfall";

type Choice = "today" | "day" | "month" | "year" | "all";

function todayIsoGuess(): string {
  return new Date().toISOString().slice(0, 10);
}
function monthIsoGuess(): string {
  return new Date().toISOString().slice(0, 7);
}
function yearIsoGuess(): string {
  return String(new Date().getFullYear());
}

function periodParams(choice: Choice, day: string, month: string, year: string): ReportPeriod {
  switch (choice) {
    case "today":
      return { period: "today" };
    case "day":
      return { day };
    case "month":
      return { month };
    case "year":
      return { year };
    case "all":
      return {};
  }
}

const OPTIONS: { value: Choice; label: string }[] = [
  { value: "today", label: "Today" },
  { value: "day", label: "Day" },
  { value: "month", label: "Month" },
  { value: "year", label: "Year" },
  { value: "all", label: "All-time" },
];

function PeriodPicker({
  choice, onChoice, day, onDay, month, onMonth, year, onYear,
}: {
  choice: Choice;
  onChoice: (c: Choice) => void;
  day: string;
  onDay: (v: string) => void;
  month: string;
  onMonth: (v: string) => void;
  year: string;
  onYear: (v: string) => void;
}) {
  return (
    <div className="period-picker" data-testid="period-picker">
      <div className="period-tabs">
        {OPTIONS.map((o) => (
          <button
            key={o.value}
            type="button"
            className={`period-tab ${choice === o.value ? "active" : ""}`}
            onClick={() => onChoice(o.value)}
          >
            {o.label}
          </button>
        ))}
      </div>
      {choice === "day" && (
        <input type="date" aria-label="pick a day" value={day} onChange={(e) => onDay(e.target.value)} />
      )}
      {choice === "month" && (
        <input
          type="month"
          aria-label="pick a month"
          value={month}
          onChange={(e) => onMonth(e.target.value)}
        />
      )}
      {choice === "year" && (
        <input
          type="number"
          aria-label="pick a year"
          value={year}
          min={2000}
          max={2100}
          onChange={(e) => onYear(e.target.value)}
        />
      )}
    </div>
  );
}

function Stat({
  label, value, kind,
}: {
  label: string;
  value: string | null | undefined;
  kind?: "money" | "percent" | "plain";
}) {
  const text =
    value == null
      ? "—"
      : kind === "percent"
        ? pct(value)
        : kind === "money"
          ? dollars(value)
          : kind === "plain"
            ? plainDollars(value)
            : Number(value).toFixed(2);
  const cls = value == null ? "" : kind === "money" || kind === "percent" ? signClass(value) : "";
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className={`stat-val ${cls}`} title={value ?? undefined}>
        {text}
      </span>
    </div>
  );
}

// UI-26 fixed layout: ① Today hero → ② Performance → ③ Execution quality →
// ④ Health. The period picker (Today/day/month/year/all-time) drives bands
// ②-④ via GET /reports/summary; the Today hero band is always literally
// today, independent of the picker.
export function ResultsPage({ entries }: { entries: EntryCard[] }) {
  const [choice, setChoice] = useState<Choice>("today");
  const [day, setDay] = useState(todayIsoGuess());
  const [month, setMonth] = useState(monthIsoGuess());
  const [year, setYear] = useState(yearIsoGuess());
  const [summary, setSummary] = useState<ReportSummary | null>(null);
  const [daily, setDaily] = useState<DailyRow[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const params = periodParams(choice, day, month, year);
  const paramsKey = JSON.stringify(params);

  useEffect(() => {
    let alive = true;
    setSummary(null);
    setDaily(null);
    setLoadError(null);
    const p = JSON.parse(paramsKey) as ReportPeriod;
    Promise.all([api.getReportSummary(p), api.getDailySeries(p)])
      .then(([s, d]) => {
        if (alive) {
          setSummary(s);
          setDaily(d);
        }
      })
      .catch((e) => {
        if (alive) setLoadError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, [paramsKey]);

  return (
    <div className="results-page" data-testid="results-page">
      <h1 className="results-title">Results</h1>

      <TodayHero entries={entries} />

      <section className="card">
        <div className="card-head">
          <h2>Period</h2>
          {summary && <TrustBadge trust={summary.trust} />}
        </div>
        <PeriodPicker
          choice={choice}
          onChoice={setChoice}
          day={day}
          onDay={setDay}
          month={month}
          onMonth={setMonth}
          year={year}
          onYear={setYear}
        />
        {summary && <PaperBanner mode={summary.mode} />}
      </section>

      {loadError && <div className="banner-error">Could not load reports — {loadError}</div>}
      {!summary && !loadError && <p className="muted">Loading…</p>}

      {summary && daily && (
        <>
          {/* ② Performance */}
          <section className="card">
            <div className="card-head">
              <h2>Performance</h2>
              <CsvButton table="daily" period={params} />
            </div>
            <div className="stat-row">
              <Stat label="Net P&L" value={summary.core.net_pnl} kind="money" />
              <Stat label="Gross P&L" value={summary.core.gross_pnl} kind="money" />
              <Stat label="Fees" value={summary.core.fees} kind="plain" />
              <Stat label="Total credit" value={summary.core.total_credit} kind="plain" />
              <Stat label="Premium capture" value={summary.core.premium_capture} kind="percent" />
              <Stat label="Day win rate" value={summary.core.day_win_rate} kind="percent" />
            </div>

            {summary.metrics.status === "unconfigured" ? (
              <p className="gap-note" data-testid="metrics-unconfigured">
                Return/risk metrics need `reporting_capital_base` configured (RPT-04, doc 06) —
                shown as unconfigured, never a fake denominator.
              </p>
            ) : (
              <>
                <div className="stat-row">
                  <Stat label="ROC" value={summary.metrics.roc} kind="percent" />
                  <Stat label="Sharpe" value={summary.metrics.sharpe} />
                  <Stat label="Sortino" value={summary.metrics.sortino} />
                  <Stat label="Profit factor" value={summary.metrics.profit_factor} />
                  <Stat label="Expectancy/entry" value={summary.metrics.expectancy_per_entry} kind="money" />
                </div>
                {summary.metrics.sharpe == null && (
                  <p className="gap-note" data-testid="sharpe-insufficient">
                    Sharpe/Sortino need {summary.metrics.min_sample_days}+ trading days (
                    {summary.metrics.sample_days} so far) — insufficient data, not zero.
                  </p>
                )}
              </>
            )}

            <h3>Equity curve &amp; drawdown</h3>
            <EquityCurve daily={daily} breaches={summary.taxonomy.contract_breaches} />

            <h3>Daily P&amp;L calendar</h3>
            <CalendarHeatmap daily={daily} />

            <h3>Records</h3>
            <RecordsBlock daily={daily} metrics={summary.metrics} />

            {summary.period_days.length > 0 && (
              <>
                <h3>Trading days in scope</h3>
                <ul className="day-links" data-testid="day-links">
                  {summary.period_days.map((d) => (
                    <li key={d}>
                      <a href={resultsDayHref(d)}>{d}</a>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </section>

          {/* ③ Execution quality */}
          <section className="card">
            <h2>Execution quality</h2>
            <h3>P&amp;L attribution waterfall (RPT-11)</h3>
            <Waterfall wf={summary.waterfall} />
            <h3>Targeting quality (RPT-05)</h3>
            <TargetingPanel />
            <h3>Slippage</h3>
            <SlippagePanels />
          </section>

          {/* ④ Health */}
          <section className="card">
            <h2>Health</h2>
            <HealthPanel health={summary.health} daily={daily} />
          </section>
        </>
      )}
    </div>
  );
}

import { Fragment, useCallback, useEffect, useLayoutEffect, useMemo, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  BarChart3,
  Brain,
  ChevronDown,
  ChevronRight,
  Cpu,
  KeyRound,
  RefreshCw,
  Server,
  TrendingUp,
} from "lucide-react";
import { api } from "@/lib/api";
import type {
  AnalyticsResponse,
  AnalyticsDailyEntry,
  AnalyticsModelEntry,
  AnalyticsSkillEntry,
  AnalyticsSourceBreakdownEntry,
  AnalyticsCredentialPoolEntry,
} from "@/lib/api";
import { timeAgo } from "@/lib/utils";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Stats } from "@nous-research/ui/ui/components/stats";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { usePageHeader } from "@/contexts/usePageHeader";
import { useI18n } from "@/i18n";
import { PluginSlot } from "@/plugins";

const PERIODS = [
  { label: "7d", days: 7 },
  { label: "30d", days: 30 },
  { label: "90d", days: 90 },
] as const;

const CHART_HEIGHT_PX = 160;

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function formatDate(day: string): string {
  try {
    const d = new Date(day + "T00:00:00");
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  } catch {
    return day;
  }
}

// ---------------------------------------------------------------------------
// Sorting
// ---------------------------------------------------------------------------

function useTableSort<T>(
  data: T[],
  defaultKey: keyof T & string,
  defaultDir: "asc" | "desc" = "desc",
) {
  const [sortKey, setSortKey] = useState<string>(defaultKey);
  const [sortDir, setSortDir] = useState<"asc" | "desc">(defaultDir);

  const sorted = useMemo(() => {
    return [...data].sort((a, b) => {
      const aVal = a[sortKey as keyof T];
      const bVal = b[sortKey as keyof T];
      // Nulls always last regardless of direction
      if (aVal === null || aVal === undefined) return 1;
      if (bVal === null || bVal === undefined) return -1;
      if (aVal === bVal) return 0;
      const cmp = aVal > bVal ? 1 : -1;
      return sortDir === "asc" ? cmp : -cmp;
    });
  }, [data, sortKey, sortDir]);

  const toggle = useCallback(
    (key: string) => {
      if (key === sortKey) {
        setSortDir((d) => (d === "asc" ? "desc" : "asc"));
      } else {
        setSortKey(key);
        setSortDir("desc");
      }
    },
    [sortKey],
  );

  return { sorted, sortKey, sortDir, toggle };
}

function SortHeader({
  label,
  col,
  sortKey,
  sortDir,
  toggle,
  className,
}: {
  label: string;
  col: string;
  sortKey: string;
  sortDir: "asc" | "desc";
  toggle: (key: string) => void;
  className?: string;
}) {
  const active = col === sortKey;
  return (
    <th
      onClick={() => toggle(col)}
      className={`cursor-pointer select-none ${className ?? ""}`}
    >
      <span className="inline-flex items-center gap-1.5 rounded px-1 -mx-1 py-0.5 hover:bg-muted/40 transition-colors">
        {label}
        {active ? (
          sortDir === "asc" ? (
            <ArrowUp className="h-3.5 w-3.5 text-foreground/80 shrink-0" />
          ) : (
            <ArrowDown className="h-3.5 w-3.5 text-foreground/80 shrink-0" />
          )
        ) : (
          <ArrowUpDown className="h-3 w-3 text-muted-foreground/40 shrink-0" />
        )}
      </span>
    </th>
  );
}



function TokenBarChart({ daily }: { daily: AnalyticsDailyEntry[] }) {
  const { t } = useI18n();
  if (daily.length === 0) return null;

  const maxTokens = Math.max(
    ...daily.map((d) => d.input_tokens + d.output_tokens),
    1,
  );

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <BarChart3 className="h-5 w-5 text-muted-foreground" />
          <CardTitle className="text-base">
            {t.analytics.dailyTokenUsage}
          </CardTitle>
        </div>
        <div className="flex items-center gap-4 text-xs text-muted-foreground">
          <div className="flex items-center gap-1.5">
            <div className="h-2.5 w-2.5 bg-[#ffe6cb]" />
            {t.analytics.input}
          </div>
          <div className="flex items-center gap-1.5">
            <div className="h-2.5 w-2.5 bg-emerald-500" />
            {t.analytics.output}
          </div>
        </div>
      </CardHeader>
      <CardContent>
        <div
          className="flex items-end gap-[2px]"
          style={{ height: CHART_HEIGHT_PX }}
        >
          {daily.map((d) => {
            const total = d.input_tokens + d.output_tokens;
            const inputH = Math.round(
              (d.input_tokens / maxTokens) * CHART_HEIGHT_PX,
            );
            const outputH = Math.round(
              (d.output_tokens / maxTokens) * CHART_HEIGHT_PX,
            );
            return (
              <div
                key={d.day}
                className="flex-1 min-w-0 group relative flex flex-col justify-end"
                style={{ height: CHART_HEIGHT_PX }}
              >
                <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-2 hidden group-hover:block z-10 pointer-events-none">
                  <div className="bg-card border border-border px-2.5 py-1.5 text-[10px] text-foreground shadow-lg whitespace-nowrap">
                    <div className="font-medium">{formatDate(d.day)}</div>
                    <div>
                      {t.analytics.input}: {formatTokens(d.input_tokens)}
                    </div>
                    <div>
                      {t.analytics.output}: {formatTokens(d.output_tokens)}
                    </div>
                    <div>
                      {t.analytics.total}: {formatTokens(total)}
                    </div>
                  </div>
                </div>

                <div
                  className="w-full bg-[#ffe6cb]/70"
                  style={{ height: Math.max(inputH, total > 0 ? 1 : 0) }}
                />

                <div
                  className="w-full bg-emerald-500/70"
                  style={{
                    height: Math.max(outputH, d.output_tokens > 0 ? 1 : 0),
                  }}
                />
              </div>
            );
          })}
        </div>

        <div className="flex justify-between mt-2 text-[10px] text-muted-foreground">
          <span>{daily.length > 0 ? formatDate(daily[0].day) : ""}</span>
          {daily.length > 2 && (
            <span>{formatDate(daily[Math.floor(daily.length / 2)].day)}</span>
          )}
          <span>
            {daily.length > 1 ? formatDate(daily[daily.length - 1].day) : ""}
          </span>
        </div>
      </CardContent>
    </Card>
  );
}


function SourceBadge({ item }: { item: AnalyticsSourceBreakdownEntry }) {
  const provider = item.billing_provider || item.serving_provider || "unknown";
  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone="secondary" className="text-[10px]">{item.serving_label || provider}</Badge>
        {item.billing_mode_label && (
          <span className="text-[10px] text-muted-foreground">{item.billing_mode_label}</span>
        )}
      </div>
      {item.serving_detail && (
        <span className="font-mono-ui text-[10px] text-muted-foreground/70 break-all">
          {item.serving_detail}
        </span>
      )}
    </div>
  );
}

function SourceBreakdownRows({ rows, colSpan, showDay = false }: { rows: AnalyticsSourceBreakdownEntry[]; colSpan: number; showDay?: boolean }) {
  const { t } = useI18n();
  if (rows.length === 0) return null;
  return (
    <tr className="border-b border-border/50 bg-secondary/10">
      <td colSpan={colSpan} className="px-4 py-3">
        <div className="overflow-x-auto rounded border border-border/60 bg-card/40">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border/50 text-muted-foreground">
                {showDay && <th className="px-3 py-2 text-left font-medium">{t.analytics.date}</th>}
                <th className="px-3 py-2 text-left font-medium">{t.analytics.model}</th>
                <th className="px-3 py-2 text-left font-medium">{t.analytics.source}</th>
                <th className="px-3 py-2 text-left font-medium">{t.analytics.serving}</th>
                <th className="px-3 py-2 text-right font-medium">{t.sessions.title}</th>
                <th className="px-3 py-2 text-right font-medium">{t.analytics.tokens}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((item, idx) => (
                <tr key={`${item.day ?? "all"}-${item.model}-${item.source}-${item.billing_provider}-${idx}`} className="border-b border-border/30 last:border-b-0">
                  {showDay && <td className="px-3 py-2 text-muted-foreground">{item.day ? formatDate(item.day) : "—"}</td>}
                  <td className="px-3 py-2"><span className="font-mono-ui text-[11px]">{item.model}</span></td>
                  <td className="px-3 py-2 text-muted-foreground">{item.source || "unknown"}</td>
                  <td className="px-3 py-2"><SourceBadge item={item} /></td>
                  <td className="px-3 py-2 text-right text-muted-foreground">{item.sessions}</td>
                  <td className="px-3 py-2 text-right whitespace-nowrap">
                    <span className="text-[#ffe6cb]">{formatTokens(item.input_tokens)}</span>
                    {" / "}
                    <span className="text-emerald-400">{formatTokens(item.output_tokens)}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </td>
    </tr>
  );
}

function DailyTable({ daily, breakdown }: { daily: AnalyticsDailyEntry[]; breakdown: AnalyticsSourceBreakdownEntry[] }) {
  const { t } = useI18n();
  const [expandedDays, setExpandedDays] = useState<Set<string>>(new Set());
  const { sorted, sortKey, sortDir, toggle } = useTableSort(daily, "day", "desc");
  const rowsByDay = useMemo(() => {
    const map = new Map<string, AnalyticsSourceBreakdownEntry[]>();
    for (const row of breakdown) {
      if (!row.day) continue;
      const rows = map.get(row.day) ?? [];
      rows.push(row);
      map.set(row.day, rows);
    }
    return map;
  }, [breakdown]);

  if (daily.length === 0) return null;

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <TrendingUp className="h-5 w-5 text-muted-foreground" />
          <CardTitle className="text-base">
            {t.analytics.dailyBreakdown}
          </CardTitle>
        </div>
      </CardHeader>
      <CardContent>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-muted-foreground text-xs">
                <th className="w-8 py-2 pr-2" />
                <SortHeader label={t.analytics.date} col="day" sortKey={sortKey} sortDir={sortDir} toggle={toggle} className="text-left py-2 pr-4 font-medium" />
                <SortHeader label={t.sessions.title} col="sessions" sortKey={sortKey} sortDir={sortDir} toggle={toggle} className="text-right py-2 px-4 font-medium" />
                <SortHeader label={t.analytics.input} col="input_tokens" sortKey={sortKey} sortDir={sortDir} toggle={toggle} className="text-right py-2 px-4 font-medium" />
                <SortHeader label={t.analytics.output} col="output_tokens" sortKey={sortKey} sortDir={sortDir} toggle={toggle} className="text-right py-2 pl-4 font-medium" />
              </tr>
            </thead>
            <tbody>
              {sorted.map((d) => {
                const childRows = rowsByDay.get(d.day) ?? [];
                const expanded = expandedDays.has(d.day);
                return (
                  <Fragment key={d.day}>
                    <tr className="border-b border-border/50 hover:bg-secondary/20 transition-colors">
                      <td className="py-2 pr-2">
                        <button
                          type="button"
                          className="rounded p-1 text-muted-foreground hover:bg-muted/50 hover:text-foreground"
                          title={t.analytics.expandForSources}
                          onClick={() => setExpandedDays((prev) => {
                            const next = new Set(prev);
                            if (next.has(d.day)) next.delete(d.day);
                            else next.add(d.day);
                            return next;
                          })}
                        >
                          {expanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                        </button>
                      </td>
                      <td className="py-2 pr-4 font-medium">{formatDate(d.day)}</td>
                      <td className="text-right py-2 px-4 text-muted-foreground">{d.sessions}</td>
                      <td className="text-right py-2 px-4"><span className="text-[#ffe6cb]">{formatTokens(d.input_tokens)}</span></td>
                      <td className="text-right py-2 pl-4"><span className="text-emerald-400">{formatTokens(d.output_tokens)}</span></td>
                    </tr>
                    {expanded && <SourceBreakdownRows rows={childRows} colSpan={5} />}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}

function ModelTable({ models }: { models: AnalyticsModelEntry[] }) {
  const { t } = useI18n();
  const [expandedModels, setExpandedModels] = useState<Set<string>>(new Set());
  const { sorted, sortKey, sortDir, toggle } = useTableSort(models, "input_tokens", "desc");

  if (models.length === 0) return null;

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Cpu className="h-5 w-5 text-muted-foreground" />
          <CardTitle className="text-base">
            {t.analytics.perModelBreakdown}
          </CardTitle>
        </div>
      </CardHeader>
      <CardContent>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-muted-foreground text-xs">
                <th className="w-8 py-2 pr-2" />
                <SortHeader label={t.analytics.model} col="model" sortKey={sortKey} sortDir={sortDir} toggle={toggle} className="text-left py-2 pr-4 font-medium" />
                <SortHeader label={t.sessions.title} col="sessions" sortKey={sortKey} sortDir={sortDir} toggle={toggle} className="text-right py-2 px-4 font-medium" />
                <SortHeader label={t.analytics.tokens} col="input_tokens" sortKey={sortKey} sortDir={sortDir} toggle={toggle} className="text-right py-2 pl-4 font-medium" />
              </tr>
            </thead>
            <tbody>
              {sorted.map((m) => {
                const expanded = expandedModels.has(m.model);
                const sourceRows = m.source_breakdown ?? [];
                return (
                  <Fragment key={m.model}>
                    <tr className="border-b border-border/50 hover:bg-secondary/20 transition-colors">
                      <td className="py-2 pr-2">
                        <button
                          type="button"
                          className="rounded p-1 text-muted-foreground hover:bg-muted/50 hover:text-foreground"
                          title={t.analytics.expandForSources}
                          onClick={() => setExpandedModels((prev) => {
                            const next = new Set(prev);
                            if (next.has(m.model)) next.delete(m.model);
                            else next.add(m.model);
                            return next;
                          })}
                        >
                          {expanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                        </button>
                      </td>
                      <td className="py-2 pr-4"><span className="font-mono-ui text-xs">{m.model}</span></td>
                      <td className="text-right py-2 px-4 text-muted-foreground">{m.sessions}</td>
                      <td className="text-right py-2 pl-4">
                        <span className="text-[#ffe6cb]">{formatTokens(m.input_tokens)}</span>
                        {" / "}
                        <span className="text-emerald-400">{formatTokens(m.output_tokens)}</span>
                      </td>
                    </tr>
                    {expanded && <SourceBreakdownRows rows={sourceRows} colSpan={4} />}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}


function CredentialPoolCard({ credentials }: { credentials: AnalyticsCredentialPoolEntry[] }) {
  const { t } = useI18n();
  if (credentials.length === 0) return null;
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <KeyRound className="h-5 w-5 text-muted-foreground" />
          <CardTitle className="text-base">{t.analytics.authAccounts}</CardTitle>
        </div>
      </CardHeader>
      <CardContent>
        <div className="grid gap-3 md:grid-cols-2">
          {credentials.map((cred) => (
            <div key={`${cred.provider}-${cred.id || cred.ordinal}`} className="rounded border border-border/60 bg-secondary/10 p-3">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="font-medium">{cred.provider_label || cred.provider}</span>
                    <Badge tone="secondary" className="text-[10px]">#{cred.ordinal}</Badge>
                    <Badge tone={cred.status === "ok" ? "success" : "warning"} className="text-[10px]">{cred.status}</Badge>
                  </div>
                  <div className="mt-1 text-xs text-muted-foreground">
                    {t.analytics.account}: {cred.label || cred.id || cred.source || "—"}
                  </div>
                </div>
                <Server className="mt-0.5 h-4 w-4 text-muted-foreground" />
              </div>
              <div className="mt-3 space-y-1 text-xs text-muted-foreground">
                <div>{t.analytics.serving}: <span className="text-foreground/80">{cred.serving_label}</span></div>
                {cred.base_url && <div className="font-mono-ui text-[10px] break-all">{t.analytics.endpoint}: {cred.base_url}</div>}
                {cred.last_error_message && <div className="text-warning">{cred.last_error_code ?? ""} {cred.last_error_message}</div>}
              </div>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

function SkillTable({ skills }: { skills: AnalyticsSkillEntry[] }) {
  const { t } = useI18n();
  const { sorted, sortKey, sortDir, toggle } = useTableSort(skills, "total_count", "desc");

  if (skills.length === 0) return null;

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Brain className="h-5 w-5 text-muted-foreground" />
          <CardTitle className="text-base">{t.analytics.topSkills}</CardTitle>
        </div>
      </CardHeader>
      <CardContent>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-muted-foreground text-xs">
                <SortHeader label={t.analytics.skill} col="skill" sortKey={sortKey} sortDir={sortDir} toggle={toggle} className="text-left py-2 pr-4 font-medium" />
                <SortHeader label={t.analytics.loads} col="view_count" sortKey={sortKey} sortDir={sortDir} toggle={toggle} className="text-right py-2 px-4 font-medium" />
                <SortHeader label={t.analytics.edits} col="manage_count" sortKey={sortKey} sortDir={sortDir} toggle={toggle} className="text-right py-2 px-4 font-medium" />
                <SortHeader label={t.analytics.total} col="total_count" sortKey={sortKey} sortDir={sortDir} toggle={toggle} className="text-right py-2 px-4 font-medium" />
                <SortHeader label={t.analytics.lastUsed} col="last_used_at" sortKey={sortKey} sortDir={sortDir} toggle={toggle} className="text-right py-2 pl-4 font-medium" />
              </tr>
            </thead>
            <tbody>
              {sorted.map((skill) => (
                <tr
                  key={skill.skill}
                  className="border-b border-border/50 hover:bg-secondary/20 transition-colors"
                >
                  <td className="py-2 pr-4">
                    <span className="font-mono-ui text-xs">{skill.skill}</span>
                  </td>
                  <td className="text-right py-2 px-4 text-muted-foreground">
                    {skill.view_count}
                  </td>
                  <td className="text-right py-2 px-4 text-muted-foreground">
                    {skill.manage_count}
                  </td>
                  <td className="text-right py-2 px-4">{skill.total_count}</td>
                  <td className="text-right py-2 pl-4 text-muted-foreground">
                    {skill.last_used_at ? timeAgo(skill.last_used_at) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}

export default function AnalyticsPage() {
  const [days, setDays] = useState(30);
  const [data, setData] = useState<AnalyticsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const { t } = useI18n();
  const { setAfterTitle, setEnd } = usePageHeader();

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    api
      .getAnalytics(days)
      .then(setData)
      .catch((err) => setError(String(err)))
      .finally(() => setLoading(false));
  }, [days]);

  useLayoutEffect(() => {
    const periodLabel =
      PERIODS.find((p) => p.days === days)?.label ?? `${days}d`;
    setAfterTitle(
      <span className="flex items-center gap-2">
        {loading && <Spinner className="shrink-0 text-base text-primary" />}
        <Badge tone="secondary" className="text-[10px]">
          {periodLabel}
        </Badge>
      </span>,
    );
    setEnd(
      <div className="flex w-full min-w-0 flex-wrap items-center justify-end gap-2 sm:gap-2">
        <div className="flex flex-wrap items-center gap-1.5">
          {PERIODS.map((p) => (
            <Button
              key={p.label}
              type="button"
              size="sm"
              outlined={days !== p.days}
              onClick={() => setDays(p.days)}
            >
              {p.label}
            </Button>
          ))}
        </div>
        <Button
          type="button"
          size="sm"
          outlined
          onClick={load}
          disabled={loading}
          prefix={loading ? <Spinner /> : <RefreshCw />}
        >
          {t.common.refresh}
        </Button>
      </div>,
    );
    return () => {
      setAfterTitle(null);
      setEnd(null);
    };
  }, [days, loading, load, setAfterTitle, setEnd, t.common.refresh]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="flex flex-col gap-6">
      <PluginSlot name="analytics:top" />
      {loading && !data && (
        <div className="flex items-center justify-center py-24">
          <Spinner className="text-2xl text-primary" />
        </div>
      )}

      {error && (
        <Card>
          <CardContent className="py-6">
            <p className="text-sm text-destructive text-center">{error}</p>
          </CardContent>
        </Card>
      )}

      {data && (
        <>
          <div className="grid gap-6 lg:grid-cols-2">
            <Card>
              <CardContent className="py-6">
                <Stats
                  items={[
                    {
                      label: t.analytics.totalTokens,
                      value: formatTokens(
                        data.totals.total_input + data.totals.total_output,
                      ),
                    },
                    {
                      label: t.analytics.input,
                      value: formatTokens(data.totals.total_input),
                    },
                    {
                      label: t.analytics.output,
                      value: formatTokens(data.totals.total_output),
                    },
                    {
                      label: t.analytics.totalSessions,
                      value: `${data.totals.total_sessions} (~${(data.totals.total_sessions / days).toFixed(1)}${t.analytics.perDayAvg})`,
                    },
                    {
                      label: t.analytics.apiCalls,
                      value: String(
                        data.totals.total_api_calls ??
                          data.daily.reduce((sum, d) => sum + d.sessions, 0),
                      ),
                    },
                  ]}
                />
              </CardContent>
            </Card>

            <TokenBarChart daily={data.daily} />
          </div>

          <DailyTable daily={data.daily} breakdown={data.daily_breakdown ?? []} />
          <ModelTable models={data.by_model} />
          <CredentialPoolCard credentials={data.credential_pool ?? []} />
          <SkillTable skills={data.skills.top_skills} />
        </>
      )}

      {data &&
        data.daily.length === 0 &&
        data.by_model.length === 0 &&
        data.skills.top_skills.length === 0 && (
          <Card>
            <CardContent className="py-12">
              <div className="flex flex-col items-center text-muted-foreground">
                <BarChart3 className="h-8 w-8 mb-3 opacity-40" />
                <p className="text-sm font-medium">{t.analytics.noUsageData}</p>
                <p className="text-xs mt-1 text-muted-foreground/60">
                  {t.analytics.startSession}
                </p>
              </div>
            </CardContent>
          </Card>
        )}
      <PluginSlot name="analytics:bottom" />
    </div>
  );
}

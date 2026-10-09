import { useId, useMemo, useState } from "react";
import type { Digest } from "../api";

/**
 * Items per daily digest. One measure over time, so: bars.
 *
 * Delivered digests are filled in the series hue; a digest that never went out
 * is drawn as a hollow bar with a ring, so the two states differ in fill as
 * well as color and the pair is spelled out in the legend.
 */

const WIDTH = 760;
const HEIGHT = 170;
const PAD = { top: 18, right: 8, bottom: 26, left: 32 };
const BAR_GAP = 2; // surface gap between adjacent bars
const RADIUS = 4; // rounded data-end, anchored to the baseline

interface Point {
  digest: Digest;
  date: string;
  value: number;
  delivered: boolean;
}

function niceCeiling(max: number): number {
  if (max <= 4) return 4;
  const magnitude = Math.pow(10, Math.floor(Math.log10(max)));
  for (const step of [1, 2, 2.5, 5, 10]) {
    const candidate = step * magnitude;
    if (candidate >= max) return candidate;
  }
  return 10 * magnitude;
}

/** A bar with only its top corners rounded, so the baseline stays flat. */
function barPath(x: number, y: number, width: number, height: number): string {
  const r = Math.min(RADIUS, width / 2, height);
  if (height <= 0) return "";
  return [
    `M${x},${y + height}`,
    `V${y + r}`,
    `Q${x},${y} ${x + r},${y}`,
    `H${x + width - r}`,
    `Q${x + width},${y} ${x + width},${y + r}`,
    `V${y + height}`,
    "Z",
  ].join(" ");
}

export function ItemsChart({ digests }: { digests: Digest[] }) {
  const [hovered, setHovered] = useState<number | null>(null);
  const [showTable, setShowTable] = useState(false);
  const titleId = useId();

  const points = useMemo<Point[]>(
    () =>
      [...digests]
        .sort((a, b) => a.digest_date.localeCompare(b.digest_date))
        .slice(-21)
        .map((digest) => ({
          digest,
          date: digest.digest_date,
          value: digest.item_count,
          delivered: digest.status === "sent",
        })),
    [digests],
  );

  if (points.length === 0) {
    return (
      <p className="empty">
        No digests recorded yet. Run <code>digest-bot run --force</code> once to prove the path
        works end to end.
      </p>
    );
  }

  const plotWidth = WIDTH - PAD.left - PAD.right;
  const plotHeight = HEIGHT - PAD.top - PAD.bottom;
  const slot = plotWidth / points.length;
  const barWidth = Math.max(3, Math.min(34, slot - BAR_GAP));
  const top = niceCeiling(Math.max(...points.map((point) => point.value)));
  const scaleY = (value: number) => plotHeight - (value / top) * plotHeight;
  const ticks = [0, top / 2, top];
  const peak = points.reduce((best, point) => (point.value > best.value ? point : best), points[0]);

  const hoveredPoint = hovered === null ? null : points[hovered];
  const tooltipLeft = hovered === null ? 0 : ((PAD.left + hovered * slot + slot / 2) / WIDTH) * 100;
  const tooltipTop =
    hoveredPoint === null ? 0 : ((PAD.top + scaleY(hoveredPoint.value) - 8) / HEIGHT) * 100;

  return (
    <div>
      <div className="filter-row">
        <h3 className="section-title" id={titleId} style={{ margin: 0, flex: 1 }}>
          Items per daily digest &middot; last {points.length}
        </h3>
        <span className="badge" style={{ background: "transparent" }}>
          <span
            className="dot"
            style={{ background: "var(--series-1)", borderRadius: 2 }}
            aria-hidden="true"
          />
          Delivered
        </span>
        <span className="badge" style={{ background: "transparent" }}>
          <span
            className="dot"
            style={{
              background: "var(--surface-1)",
              border: "2px solid var(--status-critical)",
              borderRadius: 2,
            }}
            aria-hidden="true"
          />
          Not delivered
        </span>
        <button
          type="button"
          className="btn"
          aria-pressed={showTable}
          onClick={() => setShowTable((previous) => !previous)}
        >
          {showTable ? "Hide table" : "Show table"}
        </button>
      </div>

      <div className="chart-wrap">
        <svg
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          width="100%"
          height={HEIGHT}
          role="img"
          aria-labelledby={titleId}
          onMouseLeave={() => setHovered(null)}
        >
          {ticks.map((tick) => (
            <g key={tick}>
              <line
                x1={PAD.left}
                x2={WIDTH - PAD.right}
                y1={PAD.top + scaleY(tick)}
                y2={PAD.top + scaleY(tick)}
                stroke={tick === 0 ? "var(--axis)" : "var(--gridline)"}
                strokeWidth={1}
              />
              <text
                x={PAD.left - 7}
                y={PAD.top + scaleY(tick) + 3.5}
                textAnchor="end"
                fontSize={10}
                fill="var(--text-muted)"
                style={{ fontVariantNumeric: "tabular-nums" }}
              >
                {tick}
              </text>
            </g>
          ))}

          {points.map((point, index) => {
            const x = PAD.left + index * slot + (slot - barWidth) / 2;
            const y = PAD.top + scaleY(point.value);
            const height = plotHeight - scaleY(point.value);
            const isHovered = hovered === index;
            return (
              <g key={point.digest.id}>
                {height > 0 ? (
                  <path
                    d={barPath(x, y, barWidth, height)}
                    fill={point.delivered ? "var(--series-1)" : "var(--surface-1)"}
                    stroke={point.delivered ? "none" : "var(--status-critical)"}
                    strokeWidth={point.delivered ? 0 : 2}
                    opacity={hovered === null || isHovered ? 1 : 0.55}
                  />
                ) : (
                  /* A zero-item digest still happened: show a baseline stub. */
                  <rect
                    x={x}
                    y={PAD.top + plotHeight - 2}
                    width={barWidth}
                    height={2}
                    rx={1}
                    fill={point.delivered ? "var(--series-1)" : "var(--status-critical)"}
                    opacity={hovered === null || isHovered ? 1 : 0.55}
                  />
                )}
                {/* Hit target spans the whole slot, taller than the mark. */}
                <rect
                  className="bar-hit"
                  x={PAD.left + index * slot}
                  y={PAD.top}
                  width={slot}
                  height={plotHeight}
                  onMouseEnter={() => setHovered(index)}
                />
              </g>
            );
          })}

          {/* Selective direct label: the peak only, never every bar. */}
          {peak.value > 0 && hovered === null ? (
            <text
              x={PAD.left + points.indexOf(peak) * slot + slot / 2}
              y={PAD.top + scaleY(peak.value) - 5}
              textAnchor="middle"
              fontSize={10}
              fontWeight={600}
              fill="var(--text-secondary)"
            >
              {peak.value}
            </text>
          ) : null}

          {points.map((point, index) =>
            index % Math.ceil(points.length / 7) === 0 ? (
              <text
                key={`label-${point.digest.id}`}
                x={PAD.left + index * slot + slot / 2}
                y={HEIGHT - 8}
                textAnchor="middle"
                fontSize={10}
                fill="var(--text-muted)"
              >
                {point.date.slice(5)}
              </text>
            ) : null,
          )}
        </svg>

        {hoveredPoint ? (
          <div
            className="chart-tooltip"
            style={{ left: `${tooltipLeft}%`, top: `${tooltipTop}%` }}
            role="status"
          >
            <div className="tt-date">{hoveredPoint.date}</div>
            <div className="tt-value">
              {hoveredPoint.value} item{hoveredPoint.value === 1 ? "" : "s"}
            </div>
            <div className="tt-date">
              {hoveredPoint.delivered ? "delivered" : `not delivered (${hoveredPoint.digest.status})`}
            </div>
          </div>
        ) : null}
      </div>

      {showTable ? (
        <table className="data" style={{ marginTop: 14 }}>
          <caption className="sr-only">Items per daily digest</caption>
          <thead>
            <tr>
              <th scope="col">Date</th>
              <th scope="col">Status</th>
              <th scope="col" className="num">
                Items
              </th>
            </tr>
          </thead>
          <tbody>
            {[...points].reverse().map((point) => (
              <tr key={point.digest.id}>
                <td>{point.date}</td>
                <td>{point.digest.status}</td>
                <td className="num">{point.value}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  );
}

"use client";

import "maplibre-gl/dist/maplibre-gl.css";
import "./globe.css";
import { Map as MapLibre, Marker, Popup, setWorkerUrl, type GeoJSONSource, type StyleSpecification } from "maplibre-gl";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { Camera } from "@/lib/chapters";

// MapLibre resolves its worker next to its own script, which bundling breaks.
// `npm run maplibre-worker` copies the self-contained worker into /public.
setWorkerUrl("/maplibre-gl-worker.mjs");

const HOTSPOT_YEAR = 2023;
const HALF_CELL = 0.125; // grid cells are 0.25°
const CLICK_TOLERANCE = 6; // px: dots are tiny at globe scale
const POPUP_MIN_WIDTH = 640; // px of globe: narrower, the card docks at the bottom instead of beside the cell

// Self-contained style: no third-party tiles, only our own files in /public/data.
const style: StyleSpecification = {
  version: 8,
  projection: { type: "globe" },
  sky: { "atmosphere-blend": 0 },
  sources: {
    countries: { type: "geojson", data: "/data/countries.geojson" },
    selected: { type: "geojson", data: { type: "FeatureCollection", features: [] } },
  },
  layers: [
    { id: "space", type: "background", paint: { "background-color": "#0d131c" } },
    { id: "land", type: "fill", source: "countries", paint: { "fill-color": "#16212d" } },
    { id: "borders", type: "line", source: "countries", paint: { "line-color": "#2b3b4f", "line-width": 0.6 } },
  ],
};

/** Continent labels: drawn as HTML in our own fonts, so the map needs no glyph server. English in every language. */
const CONTINENTS = [
  { name: "North America", at: [-102, 47] },
  { name: "South America", at: [-60, -14] },
  { name: "Europe", at: [16, 52] },
  { name: "Africa", at: [19, 6] },
  { name: "Asia", at: [92, 46] },
  { name: "Oceania", at: [134, -24] },
  { name: "Antarctica", at: [40, -80] },
] as const;
const LABEL_MAX_ANGLE = 70; // degrees from the view centre; beyond this a label is near or past the globe's edge
const LABEL_MAX_ZOOM = 3.5; // continent names add nothing once zoomed into a region

/** Natural Earth's CONTINENT values, as message keys under Continents. */
const CONTINENT_KEYS: Record<string, string> = {
  "North America": "northAmerica",
  "South America": "southAmerica",
  Europe: "europe",
  Africa: "africa",
  Asia: "asia",
  Oceania: "oceania",
  Antarctica: "antarctica",
  "Seven seas (open ocean)": "sevenSeas",
};

/** Written by pipeline/web_seed.py. Region fields are indexes into the lookup tables, -1 when absent. */
type HotspotFile = {
  year: number;
  countries: { iso2: string | null; name: string; continent: string | null }[];
  ecoregions: string[];
  ecozones: { en: string; fr: string }[];
  cells: [lon: number, lat: number, n: number, country: number, biome: number, ecoregion: number, ecozone: number][];
};

function toGeoJSON(file: HotspotFile): GeoJSON.FeatureCollection<GeoJSON.Point, { n: number; i: number }> {
  return {
    type: "FeatureCollection",
    features: file.cells.map(([lon, lat, n], i) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [lon, lat] },
      properties: { n, i },
    })),
  };
}

function cellOutline(lon: number, lat: number): GeoJSON.Feature<GeoJSON.Polygon> {
  const [w, e, s, n] = [lon - HALF_CELL, lon + HALF_CELL, lat - HALF_CELL, lat + HALF_CELL];
  return {
    type: "Feature",
    properties: {},
    geometry: { type: "Polygon", coordinates: [[[w, s], [e, s], [e, n], [w, n], [w, s]]] },
  };
}

/** Great-circle angle in degrees between two [lon, lat] points. */
function angleBetween([lon1, lat1]: readonly [number, number], [lon2, lat2]: readonly [number, number]) {
  const r = Math.PI / 180;
  const cos = Math.sin(lat1 * r) * Math.sin(lat2 * r) + Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.cos((lon2 - lon1) * r);
  return Math.acos(Math.min(1, Math.max(-1, cos))) / r;
}

function CellCard({ file, index, docked, onClose }: { file: HotspotFile; index: number; docked: boolean; onClose: () => void }) {
  const t = useTranslations("Map");
  const tBiome = useTranslations("Biomes");
  const tContinent = useTranslations("Continents");
  const format = useFormatter();
  const locale = useLocale();
  const [lon, lat, n, ci, biome, ei, zi] = file.cells[index];

  const country = ci >= 0 ? file.countries[ci] : null;
  const countryName = country
    ? (country.iso2 && new Intl.DisplayNames([locale], { type: "region" }).of(country.iso2)) || country.name
    : t("noCountry");
  const continentKey = country?.continent ? CONTINENT_KEYS[country.continent] : undefined;
  const coord = (v: number) => format.number(Math.abs(v), { minimumFractionDigits: 3, maximumFractionDigits: 3 });
  type Row = { label: string; value: string; data?: boolean };
  const rows: Row[] = [
    {
      label: t("location"),
      value: t("coords", { lat: coord(lat), ns: t(lat >= 0 ? "north" : "south"), lon: coord(lon), ew: t(lon >= 0 ? "east" : "west") }),
    },
    ...(continentKey ? [{ label: t("continent"), value: tContinent(continentKey) }] : []),
    ...(biome >= 0 ? [{ label: t("biome"), value: tBiome(`b${biome}`) }] : []),
    ...(ei >= 0 ? [{ label: t("ecoregion"), value: file.ecoregions[ei] }] : []),
    ...(zi >= 0 ? [{ label: t("ecozone"), value: file.ecozones[zi][locale] }] : []),
    { label: t("detections", { year: String(file.year) }), value: t("detectionsValue", { n: format.number(n) }), data: true },
  ];

  return (
    <div
      role="dialog"
      aria-label={countryName}
      className={`grid gap-2 border border-line bg-night/95 px-3 py-2.5 font-ui text-ink ${
        docked ? "absolute right-3 bottom-12 left-3 z-10" : "w-[320px]"
      }`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="grid gap-0.5">
          <span className="font-voice text-base leading-tight sm:text-lg">{countryName}</span>
          <span className="font-data text-[11px] text-mute">{t("cell")}</span>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label={t("close")}
          className="grid size-7 shrink-0 cursor-pointer place-items-center border border-line text-mute hover:border-mute hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-flare"
        >
          <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
            <path d="M1 1l8 8M9 1l-8 8" stroke="currentColor" strokeWidth="1.5" />
          </svg>
        </button>
      </div>
      {/* Label beside value keeps the card short enough to leave the globe visible on phones */}
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] items-baseline gap-x-3 gap-y-1 text-xs">
        {rows.map((r) => (
          <div key={r.label} className="contents">
            <dt className="font-data text-[10px] tracking-wide text-mute uppercase">{r.label}</dt>
            <dd className={r.data ? "font-data text-flare" : undefined}>{r.value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

export default function Globe({ camera }: { camera: Camera }) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<MapLibre | null>(null);
  const popup = useRef<Popup | null>(null);
  const labels = useRef<HTMLDivElement[]>([]);
  const initial = useRef(camera);
  const [popupNode] = useState(() => document.createElement("div")); // the popup's content; React renders into it
  const [file, setFile] = useState<HotspotFile | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [docked, setDocked] = useState(false);

  useEffect(() => {
    if (!container.current) return;
    const m = new MapLibre({
      container: container.current,
      style,
      center: initial.current.center,
      zoom: initial.current.zoom - (container.current.clientWidth < 640 ? 1 : 0),
      // Sources are credited in the story's own legend instead of MapLibre's badge
      attributionControl: false,
      cooperativeGestures: true,
    });
    map.current = m;

    // Continent labels
    // MapLibre sets the marker element's own opacity (hidden behind the globe); our fade goes on an inner element
    labels.current = CONTINENTS.map(({ name, at }) => {
      const outer = document.createElement("div");
      outer.className = "pointer-events-none";
      const el = document.createElement("div");
      el.className = "font-data text-[10px] tracking-[0.2em] whitespace-nowrap text-mute/70 uppercase transition-opacity duration-300";
      el.textContent = name;
      outer.appendChild(el);
      new Marker({ element: outer, opacity: "1", opacityWhenCovered: "0" }).setLngLat([...at]).addTo(m);
      return el;
    });
    const placeLabels = () => {
      const c = m.getCenter();
      const zoomedIn = m.getZoom() > LABEL_MAX_ZOOM;
      labels.current.forEach((el, i) => {
        const near = angleBetween([c.lng, c.lat], CONTINENTS[i].at) < LABEL_MAX_ANGLE;
        el.style.opacity = near && !zoomedIn ? "1" : "0";
      });
    };
    m.on("move", placeLabels);
    placeLabels();

    m.on("load", async () => {
      const res = await fetch(`/data/hotspots_${HOTSPOT_YEAR}.json`);
      if (!res.ok) return;
      const data: HotspotFile = await res.json();
      setFile(data);
      m.addSource("hotspots", { type: "geojson", data: toGeoJSON(data) });
      m.addLayer({
        id: "hotspots",
        type: "circle",
        source: "hotspots",
        paint: {
          // ln(n): 0 = 1 detection, 2.3 ≈ 10, 4.6 ≈ 100, 6.9 ≈ 1,000 in the year.
          // Faint single detections keep small agricultural burns from drowning out the big fires.
          "circle-color": ["interpolate", ["linear"], ["ln", ["get", "n"]], 0, "#d8432b", 3.5, "#ff7a3d", 6.5, "#ffd166"],
          "circle-radius": [
            "interpolate", ["linear"], ["zoom"],
            1, ["interpolate", ["linear"], ["ln", ["get", "n"]], 0, 0.5, 6, 1.6],
            4, ["interpolate", ["linear"], ["ln", ["get", "n"]], 0, 1.2, 6, 3.5],
            7, ["interpolate", ["linear"], ["ln", ["get", "n"]], 0, 3, 6, 7],
          ],
          "circle-opacity": ["interpolate", ["linear"], ["ln", ["get", "n"]], 0, 0.1, 2.3, 0.35, 4.6, 0.9],
        },
      });
      // The chosen cell: its true outline (visible when zoomed in) and a ring that reads at globe scale
      m.addLayer({
        id: "selected-outline",
        type: "line",
        source: "selected",
        filter: ["==", ["geometry-type"], "Polygon"],
        paint: { "line-color": "#ff7a3d", "line-width": 2 },
      });
      m.addLayer({
        id: "selected-ring",
        type: "circle",
        source: "selected",
        filter: ["==", ["geometry-type"], "Point"],
        paint: { "circle-radius": 9, "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": "#e4e8ec", "circle-stroke-width": 1.5 },
      });
    });

    // Pick the dot nearest the click, within a few pixels
    m.on("click", (e) => {
      if (!m.getLayer("hotspots")) return;
      const { x, y } = e.point;
      const hits = m.queryRenderedFeatures(
        [
          [x - CLICK_TOLERANCE, y - CLICK_TOLERANCE],
          [x + CLICK_TOLERANCE, y + CLICK_TOLERANCE],
        ],
        { layers: ["hotspots"] },
      );
      if (!hits.length) return setSelected(null);
      const dist = (f: (typeof hits)[number]) => {
        const p = m.project((f.geometry as GeoJSON.Point).coordinates as [number, number]);
        return (p.x - x) ** 2 + (p.y - y) ** 2;
      };
      const nearest = hits.reduce((a, b) => (dist(b) < dist(a) ? b : a));
      setDocked(m.getContainer().clientWidth < POPUP_MIN_WIDTH);
      setSelected(nearest.properties.i as number);
    });
    m.on("mouseenter", "hotspots", () => (m.getCanvas().style.cursor = "pointer"));
    m.on("mouseleave", "hotspots", () => (m.getCanvas().style.cursor = ""));

    return () => {
      popup.current?.remove();
      popup.current = null;
      m.remove();
      map.current = null;
    };
  }, []);

  // Outline the chosen cell and open its card beside it
  useEffect(() => {
    const m = map.current;
    const source = m?.getSource<GeoJSONSource>("selected");
    if (!m || !source || !file) return;
    const cell = selected === null ? null : file.cells[selected];
    if (!cell) {
      source.setData({ type: "FeatureCollection", features: [] });
      popup.current?.remove();
      return;
    }
    const [lon, lat] = cell;
    source.setData({
      type: "FeatureCollection",
      features: [cellOutline(lon, lat), { type: "Feature", properties: {}, geometry: { type: "Point", coordinates: [lon, lat] } }],
    });
    // Narrow globe: the card is docked at the bottom (rendered below), so no popup
    if (docked) {
      popup.current?.remove();
      return;
    }
    popup.current ??= new Popup({
      closeButton: false, // the card has its own
      closeOnClick: false, // our click handler decides
      offset: 14, // clear of the ring
      maxWidth: "none",
      className: "cell-popup",
      locationOccludedOpacity: 0, // hide when the cell turns behind the globe
      padding: { top: 92, bottom: 44, left: 12, right: 12 }, // keep clear of the legend and the chapter rail
    }).setDOMContent(popupNode);
    popup.current.setLngLat([lon, lat]).addTo(m);
  }, [selected, file, popupNode, docked]);

  useEffect(() => {
    const m = map.current;
    if (!m) return;
    setSelected(null); // a new chapter moves the camera; the old card would point at nothing
    // One zoom level wider on narrow screens so the whole region fits
    const target = { ...camera, zoom: camera.zoom - (m.getContainer().clientWidth < 640 ? 1 : 0) };
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce) m.jumpTo(target);
    else m.flyTo({ ...target, duration: 2200, essential: false });
  }, [camera]);

  // MapLibre's CSS makes its container position: relative, so the absolute box is a wrapper
  return (
    <div className="absolute inset-0">
      <div ref={container} className="h-full w-full" />
      {file &&
        selected !== null &&
        (docked ? (
          <CellCard file={file} index={selected} docked onClose={() => setSelected(null)} />
        ) : (
          createPortal(<CellCard file={file} index={selected} docked={false} onClose={() => setSelected(null)} />, popupNode)
        ))}
    </div>
  );
}

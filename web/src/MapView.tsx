import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useEffect, useRef } from "react";
import type { Place, Profile, Station, Town, VarInfo } from "./api";
import { colour, severity } from "./format";

interface Props {
  river: GeoJSON.FeatureCollection | null;
  places: Place[];
  stations: Station[];
  towns: Town[];
  profile: Profile | null;
  info: VarInfo | null;
  selectedKm: number | null;
  onSelect: (chainageKm: number) => void;
}

const STYLE: maplibregl.StyleSpecification = {
  version: 8,
  glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
  sources: {
    base: {
      type: "raster",
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      tileSize: 256,
      maxzoom: 19,
      attribution: "© OpenStreetMap contributors",
    },
  },
  layers: [{ id: "base", type: "raster", source: "base", paint: { "raster-saturation": -0.75, "raster-opacity": 0.8 } }],
};

function points<T extends { lon: number | string; lat: number | string }>(
  items: T[],
  props: (t: T) => Record<string, unknown>,
): GeoJSON.FeatureCollection {
  return {
    type: "FeatureCollection",
    features: items.map((t) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [Number(t.lon), Number(t.lat)] },
      properties: props(t),
    })),
  };
}

export default function MapView({ river, places, stations, towns, profile, info, selectedKm, onSelect }: Props) {
  const el = useRef<HTMLDivElement>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const ready = useRef(false);
  const select = useRef(onSelect);
  select.current = onSelect;

  useEffect(() => {
    if (!el.current || map.current) return;
    const m = new maplibregl.Map({
      container: el.current,
      style: STYLE,
      bounds: [77.6, 24.6, 85.8, 30.4],
      fitBoundsOptions: { padding: 24 },
      attributionControl: { compact: true },
    });
    m.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    m.on("load", () => {
      const empty: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };
      for (const id of ["river", "places", "stations", "towns", "selected"]) m.addSource(id, { type: "geojson", data: empty });
      m.addLayer({ id: "river-casing", type: "line", source: "river", paint: { "line-color": "#17323a", "line-width": 9, "line-opacity": 0.55 }, layout: { "line-cap": "round", "line-join": "round" } });
      m.addLayer({ id: "river", type: "line", source: "river", paint: { "line-color": ["coalesce", ["get", "colour"], "#8aa"], "line-width": 6 }, layout: { "line-cap": "round", "line-join": "round" } });
      m.addLayer({ id: "river-hit", type: "line", source: "river", paint: { "line-color": "#000", "line-opacity": 0, "line-width": 22 } });
      m.addLayer({ id: "towns", type: "circle", source: "towns", paint: { "circle-radius": ["interpolate", ["linear"], ["get", "sewage"], 0, 3, 450, 13], "circle-color": "#6b4f9e", "circle-opacity": 0.25, "circle-stroke-color": "#6b4f9e", "circle-stroke-width": 1 } });
      m.addLayer({ id: "stations", type: "circle", source: "stations", paint: { "circle-radius": 2.5, "circle-color": "#fff", "circle-stroke-color": "#17323a", "circle-stroke-width": 1 } });
      m.addLayer({ id: "places", type: "circle", source: "places", paint: { "circle-radius": 5, "circle-color": "#17323a", "circle-stroke-color": "#fff", "circle-stroke-width": 1.5 } });
      m.addLayer({ id: "place-labels", type: "symbol", source: "places", minzoom: 6.3, layout: { "text-field": ["get", "name"], "text-size": 11, "text-offset": [0, 1.1], "text-anchor": "top", "text-font": ["Open Sans Semibold"] }, paint: { "text-color": "#17323a", "text-halo-color": "#fff", "text-halo-width": 1.4 } });
      m.addLayer({ id: "selected", type: "circle", source: "selected", paint: { "circle-radius": 10, "circle-color": "rgba(255,255,255,0.2)", "circle-stroke-color": "#111", "circle-stroke-width": 3 } });
      m.on("click", "river-hit", (e) => {
        const km = e.features?.[0]?.properties?.chainage_km;
        if (km !== undefined) select.current(Number(km));
      });
      m.on("click", "places", (e) => {
        const km = e.features?.[0]?.properties?.chainage_km;
        if (km !== undefined) select.current(Number(km));
      });
      for (const id of ["river-hit", "places"]) {
        m.on("mouseenter", id, () => (m.getCanvas().style.cursor = "pointer"));
        m.on("mouseleave", id, () => (m.getCanvas().style.cursor = ""));
      }
      ready.current = true;
      m.fire("ganga-ready");
    });
    map.current = m;
    return () => {
      m.remove();
      map.current = null;
      ready.current = false;
    };
  }, []);

  const whenReady = (fn: (m: maplibregl.Map) => void) => {
    const m = map.current;
    if (!m) return;
    if (ready.current) fn(m);
    else m.once("ganga-ready", () => fn(m));
  };

  useEffect(() => {
    whenReady((m) => {
      (m.getSource("places") as maplibregl.GeoJSONSource).setData(
        points(places.filter((p) => p.kind === "node"), (p) => ({ name: p.name, chainage_km: p.chainage_km })),
      );
      (m.getSource("stations") as maplibregl.GeoJSONSource).setData(points(stations, (s) => ({ code: s.code })));
      (m.getSource("towns") as maplibregl.GeoJSONSource).setData(points(towns, (t) => ({ name: t.town, sewage: Number(t.sewage_mld) })));
    });
  }, [places, stations, towns]);

  useEffect(() => {
    if (!river) return;
    whenReady((m) => {
      const data: GeoJSON.FeatureCollection = {
        type: "FeatureCollection",
        features: river.features.map((f, i) => {
          const v = profile?.p50[i];
          const c = v === undefined || !info ? undefined : colour(severity(v, profile!.variable, info));
          return { ...f, properties: { ...f.properties, colour: c } };
        }),
      };
      (m.getSource("river") as maplibregl.GeoJSONSource).setData(data);
    });
  }, [river, profile, info]);

  useEffect(() => {
    if (!river || selectedKm === null) return;
    whenReady((m) => {
      const i = Math.min(river.features.length - 1, Math.max(0, Math.floor(selectedKm / 5)));
      const line = (river.features[i].geometry as GeoJSON.LineString).coordinates;
      const mid = line[Math.floor(line.length / 2)];
      (m.getSource("selected") as maplibregl.GeoJSONSource).setData({
        type: "FeatureCollection",
        features: [{ type: "Feature", geometry: { type: "Point", coordinates: mid }, properties: {} }],
      });
    });
  }, [river, selectedKm]);

  return <div ref={el} className="map" aria-label="Map of the Ganga from Haridwar to Patna" />;
}

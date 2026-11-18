/* Shared calculation for the map, selected point, history and export. */
(function (root) {
  const key = (x, y) => Number(x).toFixed(2) + ',' + Number(y).toFixed(2);
  const grid = Array.from({length:16}, (_, i) => ({
    x: Number((3 + (i % 4) * 195.2 / 3).toFixed(2)),
    y: Number((3 + Math.floor(i / 4) * 176.5 / 3).toFixed(2)),
    row: Math.floor(i / 4), column: i % 4
  }));
  const difference = (reading, reference) => reading && reference
    ? Number((reading.z - reference.z).toFixed(4)) : null;

  function fromMesh(mesh) {
    return grid.map(p => ({...p, z:mesh.mesh_rows_as_reported[p.row][p.column],
      time:mesh.measured_at || null, source:'full_scan', session:mesh.session}));
  }

  function current(state) {
    const points = new Map();
    if (!state.connected || !state.session) return {points, reference:null};
    const mesh = state.session_mesh?.session === state.session ? state.session_mesh : null;
    if (mesh) for (const r of fromMesh(mesh)) points.set(key(r.x,r.y),r);
    for (const r of state.readings || []) {
      if (r.session === state.session) points.set(key(r.x,r.y), {...r,source:'point'});
    }
    // The latest point at front-left always wins over the full-scan value.
    const reference = points.get(key(3,3)) || null;
    return {points, reference};
  }

  function mapView(state) {
    if (state.connected && state.session) return {...current(state), mode:'live'};
    const points = new Map();
    if (state.mesh) for (const r of fromMesh(state.mesh)) points.set(key(r.x,r.y),r);
    return {points, reference:points.get(key(3,3)) || null, mode:'saved'};
  }

  const api = {key, grid, difference, current, mapView};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.PointHeights = api;
})(typeof window !== 'undefined' ? window : globalThis);

//#region lib/index.js
/**
 * MTLS Client Harness — host half. Pure UI plugin: the empty apply exists so
 * the package appears as a loader entry (seated by cordis.patch.yml); the
 * browser half ships via exports["./client"], discovered through the
 * package.json `dsh.client` declaration and served by the client-modules
 * node half. No host services are required.
 */
function apply() {}
//#endregion
export { apply };

/* ESM facade over the vendored Motion browser build.
 *
 * The `motion` package has no single-file browser bundle: its DOM entry
 * (`motion` -> `framer-motion/dom`) is a tree of ES modules with bare
 * specifiers. The same package ships a ready UMD bundle at
 * node_modules/framer-motion/dist/dom.js, which is copied here as
 * vendor/motion.js and assigns `globalThis.Motion` when evaluated.
 * This module re-exports the pieces the page uses, so main.js can import
 * them the same way it would import from the npm package.
 */
import "./motion.js";

const Motion = globalThis.Motion || {};

if (typeof Motion.animate !== "function") {
  throw new Error("motion vendor bundle did not initialise");
}

export const animate = Motion.animate;
export const stagger = Motion.stagger;
export const inView = Motion.inView;

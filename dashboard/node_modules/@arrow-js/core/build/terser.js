import { fileURLToPath } from 'url'
import fs from 'fs'
import { minify } from 'terser'

const inputFile = fileURLToPath(new URL('../dist/index.mjs', import.meta.url))
const outputFile = fileURLToPath(
  new URL('../dist/index.min.mjs', import.meta.url)
)

let result = await minify(fs.readFileSync(inputFile, 'utf-8'), {
  mangle: {
    module: true,
    reserved: [
      // '$on',
      // '$off',
      // '_em',
      // 'key',
      // 't',
      // 'r',
      // 'w',
      // 'html',
    ],
    properties: {
      reserved: ['$on', '$off', 'key'],
    },
  },
  compress: {
    ecma: '2022',
    passes: 6,
    pure_getters: true,
    unsafe: true,
    unsafe_arrows: true,
    unsafe_methods: true,
    hoist_props: true,
    collapse_vars: true,
    reduce_vars: true,
    booleans_as_integers: true,
    sequences: true,
    toplevel: true,
  },
  ecma: '2022',
  module: true,
  toplevel: true,
})

// Output the minified file.
fs.writeFileSync(outputFile, result.code, 'utf8')

// IIFE
const iifeInputFile = fileURLToPath(
  new URL('../dist/index.js', import.meta.url)
)
const iifeOutputFile = fileURLToPath(
  new URL('../dist/index.min.js', import.meta.url)
)

let iifeResult = await minify(fs.readFileSync(iifeInputFile, 'utf-8'), {
  mangle: {
    module: true,
    reserved: [
      '$on',
      '$off',
      '_em',
      'key',
      't',
      'r',
      'w',
      'html',
      '$arrow',
    ],
  },
  compress: {
    ecma: '2015',
    passes: 6,
    pure_getters: true,
    unsafe: true,
    unsafe_arrows: true,
    unsafe_methods: true,
    hoist_props: true,
    collapse_vars: true,
    reduce_vars: true,
    booleans_as_integers: true,
    sequences: true,
    toplevel: true,
  },
  ecma: '2015',
  toplevel: true,
})

// Output the minified file.
fs.writeFileSync(iifeOutputFile, iifeResult.code, 'utf8')

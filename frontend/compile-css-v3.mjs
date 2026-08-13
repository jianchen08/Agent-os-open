import postcss from 'postcss'
import tailwindcss from '@tailwindcss/postcss'
import fs from 'fs'

const inputPath = '/workspace/frontend/.v3verify/input.css'
const outputPath = '/workspace/docs/working/tool_card_ui_fix_v3_verify/tool-card-v3.css'
const input = fs.readFileSync(inputPath, 'utf8')
const result = await postcss([tailwindcss()]).process(input, { from: inputPath })
fs.writeFileSync(outputPath, result.css)
console.log('CSS compiled, bytes:', result.css.length)

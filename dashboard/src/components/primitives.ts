// Tile + meta primitives shared by dashboard sections.
import { html } from "@arrow-js/core";
import type { ArrowTemplate } from "@arrow-js/core";

export const Tile = (label: string, value: string, mini?: string) =>
  html`<article><small>${label}</small><div class="value">${value}</div>${mini ? html`<p class="mini">${mini}</p>` : null}</article>`;

export const Meta = (label: string, value: string) => html`<div><b>${label}</b><span>${value}</span></div>`;

export const Section = (title: string, body: ArrowTemplate | string) =>
  html`<section><h2>${title}</h2>${body}</section>`;
import { w as writeExpressions, c as createExpressionBlock, g as getHydrationCapture, r as registerHydrationHook, i as isTpl, a as watch, s as setAttr, b as isCmp, e as expressionPool, d as isChunk, f as adoptCapturedChunk, h as createPropsProxy, j as releaseExpressions, o as onExpressionUpdate, k as swapCleanupCollector } from './chunks/internal-DchK7S7v.mjs';
export { l as c, l as component, n as nextTick, m as onCleanup, p as pick, p as props, q as r, q as reactive } from './chunks/internal-DchK7S7v.mjs';

const eventBindingsKey = Symbol();
let bindingStackPos = -1;
const bindingStack = [];
const nodeStack = [];
const delimiter = '¤';
const delimiterComment = `<!--${delimiter}-->`;
const initialChunkPoolSize = 1024;
const chunkMemo = new WeakMap();
const chunkMemoByRef = new WeakMap();
const staleById = new Map();
const staleBySignature = new Map();
let chunkPoolHead;
let renderedMark = 0;
growChunkPool(initialChunkPoolSize);
function moveDOMRef(ref, parent, before) {
    let node = ref.f;
    if (!parent || !node)
        return;
    const last = ref.l;
    while (true) {
        const next = node === last ? null : node.nextSibling;
        parent.insertBefore(node, before || null);
        if (!next)
            return;
        node = next;
    }
}
function canSyncTemplateChunk(template, chunk) {
    return chunk.g === getChunkProto(template).g;
}
function getChunkProto(template) {
    const cached = template._p;
    if (cached)
        return cached;
    return (template._p = resolveChunkProto(template._s));
}
function resolveChunkProto(rawStrings, svg) {
    const doc = document;
    let memoByRef = svg ? undefined : chunkMemoByRef.get(rawStrings);
    const cachedByRef = memoByRef?.get(doc);
    if (cachedByRef)
        return cachedByRef;
    const signature = rawStrings.join(delimiterComment);
    const cacheKey = svg ? `${delimiter}${signature}` : signature;
    let signatureMemo = chunkMemo.get(doc);
    if (!signatureMemo) {
        signatureMemo = {};
        chunkMemo.set(doc, signatureMemo);
    }
    const cached = signatureMemo[cacheKey];
    if (cached) {
        if (!svg) {
            memoByRef ??= new WeakMap();
            memoByRef.set(doc, cached);
            chunkMemoByRef.set(rawStrings, memoByRef);
        }
        return cached;
    }
    const template = document.createElement('template');
    if (svg) {
        template.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg">${signature}</svg>`;
        const root = template.content.firstChild;
        if (root) {
            const content = template.content;
            while (root.firstChild)
                content.appendChild(root.firstChild);
            content.removeChild(root);
        }
    }
    else {
        template.innerHTML = signature;
    }
    const paths = createPaths(template.content);
    normalizeNodePlaceholders(template.content);
    const expressions = rawStrings.length - 1;
    let count = 0;
    for (let i = 0; i < paths[0].length;) {
        i += (paths[0][i + 1] ?? 0) + 3;
        count++;
    }
    if (count !== expressions) {
        throw Error('Invalid HTML position');
    }
    const created = {
        template,
        paths,
        g: cacheKey,
        expressions,
    };
    if (!svg) {
        memoByRef ??= new WeakMap();
        memoByRef.set(doc, created);
        chunkMemoByRef.set(rawStrings, memoByRef);
    }
    signatureMemo[cacheKey] = created;
    return created;
}
function syncTemplateToChunk(template, chunk, mounted = false) {
    if (chunk._t === template) {
        chunk.k = template._k;
        chunk.i = template._i;
        template._h = chunk;
        template._m = mounted;
        return;
    }
    if (chunk._t && chunk._t !== template) {
        const current = chunk._t;
        if (current._h === chunk) {
            current._m = false;
            current._h = undefined;
        }
    }
    chunk._t = template;
    chunk.k = template._k;
    chunk.i = template._i;
    template._h = chunk;
    template._m = mounted;
    writeExpressions(template._a, chunk.e);
}
function releaseTemplate(chunk) {
    const template = chunk._t;
    if (template._h === chunk) {
        template._m = false;
        template._h = undefined;
    }
}
function growChunkPool(size) {
    let head;
    let tail;
    for (let i = 0; i < size; i++) {
        const chunk = {
            paths: [[], []],
            dom: null,
            ref: { f: null, l: null },
            _t: null,
            e: -1,
            g: '',
            b: false,
            r: true,
            st: false,
            u: null,
            v: null,
            s: undefined,
            k: undefined,
            i: undefined,
            bkn: undefined,
            next: undefined,
        };
        if (tail)
            tail.next = chunk;
        else
            head = chunk;
        tail = chunk;
    }
    if (tail)
        tail.next = chunkPoolHead;
    chunkPoolHead = head;
}
function freeChunk(chunk) {
    chunk.next = chunkPoolHead;
    chunkPoolHead = chunk;
}
function configureChunk(chunk, proto, template) {
    chunk.paths = proto.paths;
    chunk.g = proto.g;
    chunk.dom = proto.template.content.cloneNode(true);
    chunk.ref.f = chunk.dom.firstChild;
    chunk.ref.l = chunk.dom.lastChild;
    chunk.e = createExpressionBlock(proto.expressions);
    chunk.b = chunk.st = false;
    chunk.r = true;
    chunk.u = chunk.v = null;
    chunk.s = chunk.bkn = undefined;
    syncTemplateToChunk(template, chunk);
}
function acquireChunk(template) {
    const proto = getChunkProto(template);
    const exact = staleById.get(template._i);
    if (exact) {
        if (exact.g !== proto.g)
            throw Error('shape mismatch');
        if (exact.r) {
            removeStaleChunk(exact);
            syncTemplateToChunk(template, exact);
            return exact;
        }
    }
    const bucket = staleBySignature.get(proto.g);
    const reused = bucket?.h;
    if (reused) {
        removeStaleChunk(reused);
        syncTemplateToChunk(template, reused);
        return reused;
    }
    if (!chunkPoolHead)
        growChunkPool(initialChunkPoolSize);
    const chunk = chunkPoolHead;
    chunkPoolHead = chunk.next;
    chunk.next = undefined;
    configureChunk(chunk, proto, template);
    return chunk;
}
function removeStaleChunk(chunk) {
    if (!chunk.st)
        return;
    const bucket = staleBySignature.get(chunk.g);
    if (bucket) {
        let previous;
        let current = bucket.h;
        while (current && current !== chunk) {
            previous = current;
            current = current.bkn;
        }
        if (current) {
            if (previous)
                previous.bkn = current.bkn;
            else
                bucket.h = current.bkn;
            if (!bucket.h)
                staleBySignature.delete(chunk.g);
        }
    }
    if (chunk.i !== undefined && staleById.get(chunk.i) === chunk) {
        staleById.delete(chunk.i);
    }
    chunk.st = false;
    chunk.bkn = undefined;
}
function dispatchChunkEvent(evt) {
    const binding = this[eventBindingsKey]?.[evt.type];
    if (!binding)
        return;
    const chunk = binding.c;
    if (!chunk._t._m)
        return;
    expressionPool[binding.p]?.(evt);
}
function getRenderableKey(renderable) {
    return (isCmp(renderable)
        ? renderable.k
        : renderable._k);
}
function html(strings, ...expSlots) {
    const template = ((el) => renderTemplate(template, el));
    template.isT = true;
    template._a = expSlots;
    template._c = ensureChunk;
    template._m = false;
    template._s = strings;
    template.key = setTemplateKey;
    template.id = setTemplateId;
    return template;
}
function svg(strings, ...expSlots) {
    const template = html(strings, ...expSlots);
    template._p = resolveChunkProto(strings, true);
    return template;
}
function ensureChunk() {
    let chunk = this._h;
    if (!chunk) {
        chunk = acquireChunk(this);
        this._h = chunk;
    }
    return chunk;
}
function setTemplateKey(key) {
    this._k = key;
    if (this._h)
        this._h.k = key;
    return this;
}
function setTemplateId(id) {
    this._i = id;
    if (this._h)
        this._h.i = id;
    return this;
}
function renderTemplate(template, el) {
    const chunk = template._c();
    if (!template._m) {
        template._m = true;
        if (!chunk.b) {
            return createBindings(chunk, el);
        }
        moveDOMRef(chunk.ref, el ?? chunk.dom);
        return el ?? chunk.dom;
    }
    moveDOMRef(chunk.ref, chunk.dom);
    return el ? el.appendChild(chunk.dom) : chunk.dom;
}
function createBindings(chunk, el) {
    const expressionPointer = chunk.e;
    const totalPaths = expressionPool[expressionPointer];
    const [pathTape, attrNames] = chunk.paths;
    const stackStart = bindingStackPos + 1;
    let tapePos = 0;
    nodeStack[0] = chunk.dom;
    for (let i = 0; i < totalPaths; i++) {
        const sharedDepth = pathTape[tapePos++];
        let remaining = pathTape[tapePos++];
        let depth = sharedDepth;
        let node = nodeStack[depth];
        while (remaining--) {
            node = node.childNodes[pathTape[tapePos++]];
            nodeStack[++depth] = node;
        }
        bindingStack[++bindingStackPos] = node;
        bindingStack[++bindingStackPos] = pathTape[tapePos++];
    }
    const stackEnd = bindingStackPos;
    for (let s = stackStart, e = expressionPointer + 1; s < stackEnd; s++, e++) {
        const node = bindingStack[s];
        const segment = bindingStack[++s];
        if (segment)
            createAttrBinding(node, attrNames[segment - 1], e, chunk);
        else
            createNodeBinding(node, e, chunk);
    }
    bindingStack.length = stackStart;
    bindingStackPos = stackStart - 1;
    chunk.b = true;
    return el ? el.appendChild(chunk.dom) && el : chunk.dom;
}
function createNodeBinding(node, expressionPointer, parentChunk) {
    let fragment;
    const expression = expressionPool[expressionPointer];
    const capture = getHydrationCapture();
    const textNode = node.nodeType === 3 ? node : null;
    if (isCmp(expression) || isTpl(expression) || Array.isArray(expression)) {
        parentChunk.r = false;
        const render = createRenderFn(capture);
        fragment = render(expression);
        if (capture) {
            registerHydrationHook(parentChunk, (map, visited) => {
                render.adopt(map, visited);
            });
        }
    }
    else if (typeof expression === 'function') {
        let target = textNode;
        let render = null;
        const [frag, stop] = watch(expressionPointer, (value) => {
            if (!render) {
                if (isCmp(value) || isTpl(value) || Array.isArray(value)) {
                    parentChunk.r = false;
                    render = createRenderFn(capture);
                    const next = render(value);
                    if (target) {
                        target.parentNode?.replaceChild(next, target);
                        target = null;
                    }
                    return next;
                }
                if (!target)
                    target = document.createTextNode('');
                const next = renderText(value);
                if (target.nodeValue !== next)
                    target.nodeValue = next;
                return target;
            }
            return render(value);
        });
        (parentChunk.u ??= []).push(stop);
        fragment = frag;
        if (capture) {
            registerHydrationHook(parentChunk, (map, visited) => {
                if (target) {
                    const adopted = map.get(target);
                    if (adopted)
                        target = adopted;
                }
                render?.adopt(map, visited);
            });
        }
    }
    else {
        let target = textNode ?? document.createTextNode('');
        target.data = renderText(expression);
        fragment = target;
        if (capture) {
            onExpressionUpdate(expressionPointer, (value) => (target.data = renderText(value)));
            registerHydrationHook(parentChunk, (map) => {
                const adopted = map.get(target);
                if (adopted)
                    target = adopted;
            });
        }
        else {
            onExpressionUpdate(expressionPointer, target);
        }
    }
    if (node === parentChunk.ref.f || node === parentChunk.ref.l) {
        const last = fragment.nodeType === 11
            ? fragment.lastChild
            : fragment;
        if (node === parentChunk.ref.f) {
            parentChunk.ref.f =
                fragment.nodeType === 11
                    ? fragment.firstChild
                    : fragment;
        }
        if (node === parentChunk.ref.l)
            parentChunk.ref.l = last;
    }
    if (fragment !== node)
        node.parentNode?.replaceChild(fragment, node);
}
function createAttrBinding(node, attrName, expressionPointer, parentChunk) {
    if (node.nodeType !== 1)
        return;
    let target = node;
    const expression = expressionPool[expressionPointer];
    const capture = getHydrationCapture();
    if (attrName[0] === '@') {
        const event = attrName.slice(1);
        const bindings = (target[eventBindingsKey] ??= {});
        bindings[event] = { c: parentChunk, p: expressionPointer };
        const record = [target, event];
        target.addEventListener(event, dispatchChunkEvent);
        target.removeAttribute(attrName);
        (parentChunk.v ??= []).push(record);
        if (capture) {
            registerHydrationHook(parentChunk, (map) => {
                const adopted = map.get(target);
                if (!adopted)
                    return;
                const previousTarget = target;
                const previousBindings = previousTarget[eventBindingsKey];
                if (previousBindings) {
                    delete previousBindings[event];
                    let hasBindings = false;
                    for (const key in previousBindings) {
                        hasBindings = true;
                        break;
                    }
                    if (!hasBindings)
                        delete previousTarget[eventBindingsKey];
                }
                target.removeEventListener(event, dispatchChunkEvent);
                target = adopted;
                record[0] = target;
                const nextBindings = (target[eventBindingsKey] ??= {});
                nextBindings[event] = { c: parentChunk, p: expressionPointer };
                target.addEventListener(event, dispatchChunkEvent);
                target.removeAttribute(attrName);
            });
        }
    }
    else if (typeof expression === 'function' && !isTpl(expression)) {
        const [, stop] = watch(expressionPointer, (value) => setAttr(target, attrName, value));
        (parentChunk.u ??= []).push(stop);
        if (capture) {
            registerHydrationHook(parentChunk, (map) => {
                const adopted = map.get(target);
                if (adopted)
                    target = adopted;
            });
        }
    }
    else {
        setAttr(target, attrName, expression);
        if (capture) {
            onExpressionUpdate(expressionPointer, (value) => setAttr(target, attrName, value));
        }
        else {
            onExpressionUpdate(expressionPointer, target, attrName);
        }
    }
}
function createRenderFn(capture) {
    let previous;
    let keyedChunks = Object.create(null);
    const render = function render(renderable) {
        if (!previous) {
            if (isCmp(renderable)) {
                const [fragment, chunk] = renderComponent(renderable);
                previous = mountChunkFragment(fragment, chunk);
                return fragment;
            }
            if (isTpl(renderable)) {
                const fragment = renderable();
                previous = mountChunkFragment(fragment, renderable._h);
                return fragment;
            }
            if (Array.isArray(renderable)) {
                const [fragment, rendered] = renderList(renderable);
                previous = rendered;
                return fragment;
            }
            return (previous = document.createTextNode(renderText(renderable)));
        }
        if (Array.isArray(renderable)) {
            if (!Array.isArray(previous)) {
                const [fragment, nextList] = renderList(renderable);
                getNode(previous).after(fragment);
                forgetChunk(previous);
                unmount(previous);
                previous = nextList;
            }
            else {
                let i = 0;
                const renderableLength = renderable.length;
                const previousLength = previous.length;
                if (renderableLength &&
                    previousLength === 1 &&
                    !isChunk(previous[0]) &&
                    !previous[0].data) {
                    const [fragment, rendered] = renderList(renderable);
                    previous[0].replaceWith(fragment);
                    previous = rendered;
                    return;
                }
                if (renderableLength === previousLength) {
                    const renderedList = new Array(renderableLength);
                    for (; i < renderableLength; i++) {
                        const item = renderable[i];
                        if ((isCmp(item) && item.k !== undefined) || (isTpl(item) && item._k !== undefined)) {
                            i = -1;
                            break;
                        }
                        const prev = previous[i];
                        if (isTpl(item) &&
                            isChunk(prev) &&
                            prev._t === item &&
                            item._h === prev &&
                            item._m) {
                            renderedList[i] = prev;
                            continue;
                        }
                        if (isTpl(item) && isChunk(prev)) {
                            const template = item;
                            const proto = template._p ?? getChunkProto(template);
                            if (prev.g === proto.g) {
                                syncTemplateToChunk(template, prev, true);
                                renderedList[i] = prev;
                                continue;
                            }
                        }
                        renderedList[i] = patch(item, prev);
                    }
                    if (i === renderableLength) {
                        previous = renderedList;
                        return;
                    }
                    i = 0;
                }
                const keyedList = patchKeyedList(renderable, previous);
                if (keyedList) {
                    previous = keyedList;
                    return;
                }
                if (renderableLength > previousLength && previousLength) {
                    for (; i < previousLength; i++) {
                        const item = renderable[i];
                        const prev = previous[i];
                        if (isTpl(item) &&
                            isChunk(prev) &&
                            prev._t === item &&
                            item._h === prev &&
                            item._m) {
                            continue;
                        }
                        i = -1;
                        break;
                    }
                    if (i === previousLength) {
                        const fragment = document.createDocumentFragment();
                        const renderedList = previous.slice();
                        for (i = previousLength; i < renderableLength; i++) {
                            renderedList[i] = mountItem(renderable[i], fragment);
                        }
                        getNode(previous[previousLength - 1]).after(fragment);
                        previous = renderedList;
                        return;
                    }
                    i = 0;
                }
                let anchor;
                const renderedList = [];
                const mark = ++renderedMark;
                const updaterFrag = renderableLength > previousLength
                    ? document.createDocumentFragment()
                    : null;
                for (; i < renderableLength; i++) {
                    let item = renderable[i];
                    const prev = previous[i];
                    let key;
                    if (isTpl(item) &&
                        (key = item._k) !== undefined &&
                        key in keyedChunks) {
                        const keyedChunk = keyedChunks[key];
                        if (canSyncTemplateChunk(item, keyedChunk)) {
                            syncTemplateToChunk(item, keyedChunk, true);
                            item = keyedChunk._t;
                        }
                    }
                    if (i > previousLength - 1) {
                        renderedList[i] = mountItem(item, updaterFrag);
                        continue;
                    }
                    if (isTpl(item) &&
                        isChunk(prev) &&
                        prev._t === item &&
                        item._h === prev &&
                        item._m) {
                        anchor = getNode(prev);
                        renderedList[i] = prev;
                        prev.mk = mark;
                        continue;
                    }
                    const used = patch(item, prev, anchor);
                    anchor = getNode(used);
                    renderedList[i] = used;
                    used.mk = mark;
                }
                if (!renderableLength) {
                    const placeholder = (renderedList[0] = document.createTextNode(''));
                    const sync = canSyncUnmount(previous);
                    const detached = sync && replaceListWithPlaceholder(previous, placeholder);
                    if (!detached)
                        getNode(previous).after(placeholder);
                    keyedChunks = Object.create(null);
                    if (sync)
                        removeUnmounted(previous, detached);
                    else
                        unmount(previous);
                    previous = renderedList;
                    return;
                }
                else if (renderableLength > previousLength) {
                    anchor?.after(updaterFrag);
                }
                for (i = 0; i < previousLength; i++) {
                    const stale = previous[i];
                    if (stale.mk === mark)
                        continue;
                    forgetChunk(stale);
                    unmount(stale);
                }
                previous = renderedList;
            }
        }
        else {
            if (Array.isArray(previous))
                keyedChunks = Object.create(null);
            previous = patch(renderable, previous);
        }
    };
    render.adopt = capture
        ? (map, visited) => {
            previous = adoptRenderedValue(previous, capture, map, visited);
        }
        : () => { };
    function renderList(renderable) {
        const fragment = document.createDocumentFragment();
        if (!renderable.length) {
            const placeholder = document.createTextNode('');
            fragment.appendChild(placeholder);
            return [fragment, [placeholder]];
        }
        const renderedItems = new Array(renderable.length);
        for (let i = 0; i < renderable.length; i++) {
            renderedItems[i] = mountItem(renderable[i], fragment);
        }
        return [fragment, renderedItems];
    }
    function syncComponentChunk(renderable, chunk) {
        if (chunk.s?.[1] !== renderable.h)
            return false;
        if (chunk.s[0] !== renderable.p)
            chunk.s[0] = renderable.p;
        if (chunk.s[2] !== renderable.e)
            chunk.s[2] = renderable.e;
        return true;
    }
    function syncKeyedRenderable(renderable, chunk) {
        if (isCmp(renderable))
            return syncComponentChunk(renderable, chunk);
        if (!canSyncTemplateChunk(renderable, chunk))
            return false;
        syncTemplateToChunk(renderable, chunk, true);
        return true;
    }
    function moveChunkIntoPlace(chunk, prev, anchor) {
        if (anchor) {
            moveDOMRef(chunk.ref, anchor.parentNode, anchor.nextSibling);
            return;
        }
        const target = getNode(prev, undefined, true);
        moveDOMRef(chunk.ref, target.parentNode, target);
    }
    function patchKeyedList(renderable, previousList) {
        const renderableLength = renderable.length;
        const previousLength = previousList.length;
        if (!renderableLength) {
            const placeholder = document.createTextNode('');
            const sync = canSyncUnmount(previousList);
            const detached = sync && replaceListWithPlaceholder(previousList, placeholder);
            if (!detached)
                getNode(previousList).after(placeholder);
            keyedChunks = Object.create(null);
            if (sync)
                removeUnmounted(previousList, detached);
            else
                unmount(previousList);
            return [placeholder];
        }
        const renderedList = new Array(renderableLength);
        const parent = getNode(previousList[0]).parentNode;
        if (!parent)
            return null;
        let sharedPrefix = 0;
        const sharedPrefixKeys = Object.create(null);
        for (; sharedPrefix < previousLength && sharedPrefix < renderableLength; sharedPrefix++) {
            const rendered = previousList[sharedPrefix];
            if (!isChunk(rendered) || rendered.k === undefined)
                return null;
            const item = renderable[sharedPrefix];
            if (!isCmp(item) && !isTpl(item))
                return null;
            const key = getRenderableKey(item);
            if (key === undefined || key !== rendered.k)
                break;
            sharedPrefixKeys[key] = 1;
            if (!(isTpl(item) &&
                rendered._t === item &&
                item._h === rendered &&
                item._m) &&
                !syncKeyedRenderable(item, rendered)) {
                return null;
            }
            renderedList[sharedPrefix] = rendered;
        }
        if (sharedPrefix === previousLength) {
            if (sharedPrefix === renderableLength)
                return renderedList;
            const fragment = document.createDocumentFragment();
            for (let i = sharedPrefix; i < renderableLength; i++) {
                const item = renderable[i];
                if (!isCmp(item) && !isTpl(item))
                    return null;
                const key = getRenderableKey(item);
                if (key === undefined || key in sharedPrefixKeys)
                    return null;
                sharedPrefixKeys[key] = 1;
                renderedList[i] = mountItem(item, fragment);
            }
            parent.insertBefore(fragment, previousLength
                ? getNode(previousList[previousLength - 1]).nextSibling
                : null);
            return renderedList;
        }
        if (sharedPrefix === renderableLength) {
            for (let i = sharedPrefix; i < previousLength; i++) {
                const stale = previousList[i];
                forgetChunk(stale);
                unmount(stale);
            }
            return renderedList;
        }
        let oldStart = sharedPrefix;
        let newStart = sharedPrefix;
        let oldEnd = previousLength - 1;
        let newEnd = renderableLength - 1;
        while (oldStart <= oldEnd && newStart <= newEnd) {
            const startChunk = previousList[oldStart];
            const endChunk = previousList[oldEnd];
            const startKey = startChunk.k;
            const endKey = endChunk.k;
            const nextStart = renderable[newStart];
            const nextEnd = renderable[newEnd];
            const nextStartKey = isCmp(nextStart) || isTpl(nextStart)
                ? getRenderableKey(nextStart)
                : undefined;
            const nextEndKey = isCmp(nextEnd) || isTpl(nextEnd) ? getRenderableKey(nextEnd) : undefined;
            if (nextStartKey === undefined || nextEndKey === undefined)
                return null;
            if (startKey === nextStartKey) {
                if (!(isTpl(nextStart) &&
                    startChunk._t === nextStart &&
                    nextStart._h === startChunk &&
                    nextStart._m) &&
                    !syncKeyedRenderable(nextStart, startChunk)) {
                    return null;
                }
                renderedList[newStart++] = startChunk;
                oldStart++;
                continue;
            }
            if (endKey === nextEndKey) {
                if (!(isTpl(nextEnd) &&
                    endChunk._t === nextEnd &&
                    nextEnd._h === endChunk &&
                    nextEnd._m) &&
                    !syncKeyedRenderable(nextEnd, endChunk)) {
                    return null;
                }
                renderedList[newEnd--] = endChunk;
                oldEnd--;
                continue;
            }
            if (startKey === nextEndKey) {
                if (!(isTpl(nextEnd) &&
                    startChunk._t === nextEnd &&
                    nextEnd._h === startChunk &&
                    nextEnd._m) &&
                    !syncKeyedRenderable(nextEnd, startChunk)) {
                    return null;
                }
                moveDOMRef(startChunk.ref, parent, getNode(endChunk).nextSibling);
                renderedList[newEnd--] = startChunk;
                oldStart++;
                continue;
            }
            if (endKey === nextStartKey) {
                if (!(isTpl(nextStart) &&
                    endChunk._t === nextStart &&
                    nextStart._h === endChunk &&
                    nextStart._m) &&
                    !syncKeyedRenderable(nextStart, endChunk)) {
                    return null;
                }
                moveDOMRef(endChunk.ref, parent, getNode(startChunk, undefined, true));
                renderedList[newStart++] = endChunk;
                oldEnd--;
                continue;
            }
            break;
        }
        if (newStart > newEnd) {
            for (let i = oldStart; i <= oldEnd; i++) {
                const stale = previousList[i];
                forgetChunk(stale);
                unmount(stale);
            }
            return renderedList;
        }
        if (oldStart > oldEnd) {
            const fragment = document.createDocumentFragment();
            for (let i = newStart; i <= newEnd; i++) {
                const item = renderable[i];
                if (!isCmp(item) && !isTpl(item))
                    return null;
                renderedList[i] = mountItem(item, fragment);
            }
            parent.insertBefore(fragment, newEnd + 1 < renderableLength
                ? getNode(renderedList[newEnd + 1], undefined, true)
                : null);
            return renderedList;
        }
        const previousIndexByKey = Object.create(null);
        for (let i = oldStart; i <= oldEnd; i++) {
            const rendered = previousList[i];
            if (!isChunk(rendered) || rendered.k === undefined)
                return null;
            const key = rendered.k;
            if (key in previousIndexByKey)
                return null;
            previousIndexByKey[key] = i + 1;
        }
        const middleIndexByKey = Object.create(null);
        let overlaps = 0;
        for (let i = newStart; i <= newEnd; i++) {
            const item = renderable[i];
            const key = isCmp(item) || isTpl(item) ? getRenderableKey(item) : undefined;
            if (key === undefined || key in middleIndexByKey)
                return null;
            middleIndexByKey[key] = i + 1;
            if (key in previousIndexByKey)
                overlaps++;
        }
        if (!overlaps) {
            const first = getNode(previousList[oldStart], undefined, true);
            const last = getNode(previousList[oldEnd]);
            const fragment = document.createDocumentFragment();
            for (let i = newStart; i <= newEnd; i++) {
                const item = renderable[i];
                if (!isCmp(item) && !isTpl(item))
                    return null;
                renderedList[i] = mountItem(item, fragment);
            }
            const parent = first.parentNode;
            if (parent && first === parent.firstChild && last === parent.lastChild) {
                parent.replaceChildren(fragment);
            }
            else {
                const range = document.createRange();
                range.setStartBefore(first);
                range.setEndAfter(last);
                range.deleteContents();
                range.insertNode(fragment);
            }
            for (let i = oldStart; i <= oldEnd; i++) {
                const stale = previousList[i];
                forgetChunk(stale);
                destroyChunk(stale, true);
            }
            return renderedList;
        }
        for (let i = oldStart; i <= oldEnd; i++) {
            const stale = previousList[i];
            const nextIndex = middleIndexByKey[stale.k];
            if (nextIndex === undefined) {
                forgetChunk(stale);
                unmount(stale);
                continue;
            }
            const item = renderable[nextIndex - 1];
            if (!syncKeyedRenderable(item, stale))
                return null;
            renderedList[nextIndex - 1] = stale;
        }
        let before = newEnd + 1 < renderableLength
            ? getNode(renderedList[newEnd + 1], undefined, true)
            : getNode(previousList[previousLength - 1]).nextSibling;
        for (let i = newEnd; i >= newStart; i--) {
            const existing = renderedList[i];
            if (!existing) {
                const item = renderable[i];
                if (!isCmp(item) && !isTpl(item))
                    return null;
                const fragment = document.createDocumentFragment();
                const mounted = mountItem(item, fragment);
                renderedList[i] = mounted;
                parent.insertBefore(fragment, before);
                before = getNode(mounted, undefined, true);
                continue;
            }
            const start = getNode(existing, undefined, true);
            if (start.parentNode !== parent || start.nextSibling !== before) {
                moveDOMRef(existing.ref, parent, before);
            }
            before = start;
        }
        return renderedList;
    }
    function patch(renderable, prev, anchor) {
        const nodeType = prev.nodeType ?? 0;
        if (isCmp(renderable)) {
            const key = renderable.k;
            if (key !== undefined && key in keyedChunks) {
                const keyedChunk = keyedChunks[key];
                if (syncComponentChunk(renderable, keyedChunk)) {
                    if (keyedChunk === prev)
                        return prev;
                    moveChunkIntoPlace(keyedChunk, prev, anchor);
                    return keyedChunk;
                }
            }
            else if (isChunk(prev) && syncComponentChunk(renderable, prev)) {
                if (prev.k !== renderable.k) {
                    forgetChunk(prev);
                    prev.k = renderable.k;
                    rememberKeyedChunk(prev);
                }
                return prev;
            }
            const [fragment, chunk] = renderComponent(renderable);
            const mounted = mountChunkFragment(fragment, chunk);
            getNode(prev, anchor).after(fragment);
            forgetChunk(prev);
            unmount(prev);
            rememberKeyedChunk(chunk);
            return mounted;
        }
        if (!isTpl(renderable) && nodeType === 3) {
            const value = renderText(renderable);
            if (prev.data !== value)
                prev.data = value;
            return prev;
        }
        if (isTpl(renderable)) {
            const template = renderable;
            const key = template._k;
            if (key !== undefined && key in keyedChunks) {
                const keyedChunk = keyedChunks[key];
                if (canSyncTemplateChunk(template, keyedChunk)) {
                    syncTemplateToChunk(template, keyedChunk, true);
                    if (keyedChunk === prev)
                        return prev;
                    moveChunkIntoPlace(keyedChunk, prev, anchor);
                    return keyedChunk;
                }
            }
            const proto = getChunkProto(template);
            if (isChunk(prev) && prev.g === proto.g) {
                syncTemplateToChunk(template, prev, true);
                return prev;
            }
            const fragment = renderable();
            const chunk = template._h;
            const mounted = mountChunkFragment(fragment, chunk);
            getNode(prev, anchor).after(fragment);
            forgetChunk(prev);
            unmount(prev);
            rememberKeyedChunk(chunk);
            return mounted;
        }
        const text = document.createTextNode(renderText(renderable));
        getNode(prev, anchor).after(text);
        forgetChunk(prev);
        unmount(prev);
        return text;
    }
    function mountItem(item, fragment) {
        if (isCmp(item)) {
            const [inner, chunk] = renderComponent(item);
            fragment.appendChild(inner);
            rememberKeyedChunk(chunk);
            return mountChunkFragment(fragment, chunk);
        }
        if (isTpl(item)) {
            item(fragment);
            const chunk = item._h;
            rememberKeyedChunk(chunk);
            return mountChunkFragment(fragment, chunk);
        }
        const node = document.createTextNode(renderText(item));
        fragment.appendChild(node);
        return node;
    }
    function mountChunkFragment(fragment, chunk) {
        if (chunk.ref.f)
            return chunk;
        const placeholder = document.createTextNode('');
        fragment.appendChild(placeholder);
        return placeholder;
    }
    function rememberKeyedChunk(chunk) {
        if (chunk.k !== undefined)
            keyedChunks[chunk.k] = chunk;
    }
    function forgetChunk(item) {
        if (isChunk(item) && item.k !== undefined && keyedChunks[item.k] === item) {
            delete keyedChunks[item.k];
        }
    }
    function renderComponent(renderable) {
        const [props, emit, box] = createPropsProxy(renderable.p, renderable.h, renderable.e);
        const cleanups = [];
        const previousCollector = swapCleanupCollector(cleanups);
        let template;
        let fragment;
        try {
            template = renderable.h(props, emit);
            fragment = template();
        }
        finally {
            swapCleanupCollector(previousCollector);
        }
        const chunk = template._c();
        if (cleanups.length) {
            (chunk.u ??= []).push(...cleanups);
        }
        chunk.r = false;
        chunk.s = box;
        chunk.k = renderable.k;
        return [fragment, chunk];
    }
    return render;
}
let unmountStack = [];
function destroyChunk(chunk, detached = false) {
    if (chunk.st)
        removeStaleChunk(chunk);
    releaseTemplate(chunk);
    if (chunk.v) {
        for (let i = 0; i < chunk.v.length; i++) {
            const [target, event] = chunk.v[i];
            const bindings = target[eventBindingsKey];
            if (bindings) {
                delete bindings[event];
                let hasBindings = false;
                for (const key in bindings) {
                    hasBindings = true;
                    break;
                }
                if (!hasBindings)
                    delete target[eventBindingsKey];
            }
            target.removeEventListener(event, dispatchChunkEvent);
        }
    }
    if (chunk.u) {
        for (let i = 0; i < chunk.u.length; i++)
            chunk.u[i]();
        chunk.u = null;
    }
    if (chunk.e + 1) {
        releaseExpressions(chunk.e);
        chunk.e = -1;
    }
    let node = chunk.ref.f;
    if (!detached && node) {
        const last = chunk.ref.l;
        if (node === last)
            node.remove();
        else {
            while (node) {
                const next = node === last ? null : node.nextSibling;
                node.remove();
                if (!next)
                    break;
                node = next;
            }
        }
    }
    chunk.dom.textContent = '';
    chunk.ref.f = chunk.ref.l = null;
    chunk.k = chunk.i = chunk.s = undefined;
    chunk.u = chunk.v = null;
    chunk.b = chunk.st = false;
    chunk.r = true;
    chunk.g = '';
    freeChunk(chunk);
}
function recycleChunk(chunk, detached = false) {
    if (!detached)
        moveDOMRef(chunk.ref, chunk.dom);
    releaseTemplate(chunk);
    if (chunk.st || !chunk.r)
        return;
    chunk.st = true;
    let bucket = staleBySignature.get(chunk.g);
    if (!bucket) {
        bucket = {};
        staleBySignature.set(chunk.g, bucket);
    }
    chunk.bkn = bucket.h;
    bucket.h = chunk;
    if (chunk.i !== undefined)
        staleById.set(chunk.i, chunk);
}
let unmountQueued = false;
function canSyncUnmount(chunk) {
    for (let i = 0; i < chunk.length; i++) {
        const item = chunk[i];
        if (isChunk(item) && !item.r)
            return false;
    }
    return true;
}
function replaceListWithPlaceholder(chunk, placeholder) {
    if (!chunk.length)
        return false;
    const first = getNode(chunk[0], undefined, true);
    const last = getNode(chunk[chunk.length - 1]);
    const parent = first.parentNode;
    if (!parent || first !== parent.firstChild || last !== parent.lastChild) {
        return false;
    }
    parent.replaceChildren(placeholder);
    return true;
}
function removeUnmounted(chunk, detached = false) {
    if (isChunk(chunk)) {
        if (chunk.r)
            recycleChunk(chunk, detached);
        else
            destroyChunk(chunk, detached);
        return;
    }
    if (Array.isArray(chunk)) {
        if (!detached && chunk.length) {
            const first = getNode(chunk[0], undefined, true);
            const last = getNode(chunk[chunk.length - 1]);
            const parent = first.parentNode;
            if (parent) {
                if (first === parent.firstChild && last === parent.lastChild) {
                    parent.textContent = '';
                }
                else {
                    const range = document.createRange();
                    range.setStartBefore(first);
                    range.setEndAfter(last);
                    range.deleteContents();
                }
                detached = true;
            }
        }
        let bucket;
        let signature = '';
        for (let i = 0; i < chunk.length; i++) {
            const item = chunk[i];
            if (isChunk(item)) {
                if (!item.r) {
                    destroyChunk(item, detached);
                    continue;
                }
                if (!detached)
                    moveDOMRef(item.ref, item.dom);
                releaseTemplate(item);
                if (item.st)
                    continue;
                item.st = true;
                if (signature !== item.g) {
                    signature = item.g;
                    bucket = staleBySignature.get(signature);
                    if (!bucket) {
                        bucket = {};
                        staleBySignature.set(signature, bucket);
                    }
                }
                item.bkn = bucket.h;
                bucket.h = item;
                if (item.i !== undefined)
                    staleById.set(item.i, item);
            }
            else if (!detached) {
                item.remove();
            }
        }
        return;
    }
    if (!detached)
        chunk.remove();
}
function drainUnmountStack() {
    unmountQueued = false;
    const stack = unmountStack;
    unmountStack = [];
    for (let i = 0; i < stack.length; i++)
        removeUnmounted(stack[i]);
    if (unmountStack.length)
        scheduleUnmountDrain();
}
function scheduleUnmountDrain() {
    if (unmountQueued)
        return;
    unmountQueued = true;
    queueMicrotask(drainUnmountStack);
}
function unmount(chunk) {
    if (!chunk)
        return;
    unmountStack.push(chunk);
    scheduleUnmountDrain();
}
function renderText(value) {
    return value || value === 0 ? value : '';
}
function getNode(chunk, anchor, first) {
    if (isChunk(chunk)) {
        return first ? chunk.ref.f : chunk.ref.l;
    }
    if (Array.isArray(chunk)) {
        return getNode(chunk[first ? 0 : chunk.length - 1], anchor, first);
    }
    return chunk;
}
function adoptRenderedValue(value, capture, map, visited) {
    if (!value)
        return value;
    if (isChunk(value)) {
        adoptCapturedChunk(capture, value, map, visited);
        return value;
    }
    if (Array.isArray(value)) {
        const next = new Array(value.length);
        for (let i = 0; i < value.length; i++) {
            next[i] = adoptRenderedValue(value[i], capture, map, visited);
        }
        return next;
    }
    return map.get(value) ?? value;
}
function createPaths(dom) {
    const pathTape = [];
    const attrNames = [];
    const path = [];
    const previous = [];
    const pushPath = (attrName) => {
        const pathLen = path.length;
        const previousLen = previous.length;
        const limit = pathLen < previousLen ? pathLen : previousLen;
        let sharedDepth = 0;
        while (sharedDepth < limit && previous[sharedDepth] === path[sharedDepth]) {
            sharedDepth++;
        }
        pathTape.push(sharedDepth, pathLen - sharedDepth);
        for (let i = sharedDepth; i < pathLen; i++)
            pathTape.push(path[i]);
        pathTape.push(attrName ? attrNames.push(attrName) : 0);
        previous.length = pathLen;
        for (let i = 0; i < pathLen; i++)
            previous[i] = path[i];
    };
    const walk = (node) => {
        if (node.nodeType === 1) {
            const attrs = node.attributes;
            for (let i = 0; i < attrs.length; i++) {
                const attr = attrs[i];
                if (attr.value === delimiterComment)
                    pushPath(attr.name);
            }
        }
        else if (node.nodeType === 8) {
            pushPath();
        }
        else if (node.nodeType === 3 && node.nodeValue === delimiterComment) {
            pushPath();
        }
        const children = node.childNodes;
        for (let i = 0; i < children.length; i++) {
            path.push(i);
            walk(children[i]);
            path.pop();
        }
    };
    const children = dom.childNodes;
    for (let i = 0; i < children.length; i++) {
        path.push(i);
        walk(children[i]);
        path.pop();
    }
    return [pathTape, attrNames];
}
function normalizeNodePlaceholders(dom) {
    const walk = (node) => {
        const children = node.childNodes;
        for (let i = 0; i < children.length; i++) {
            const child = children[i];
            if (child.nodeType === 8 && child.data === delimiter) {
                node.replaceChild(document.createTextNode(''), child);
                continue;
            }
            if (child.nodeType === 3 && child.nodeValue === delimiterComment) {
                child.nodeValue = '';
            }
            if (child.firstChild)
                walk(child);
        }
    };
    walk(dom);
}

export { html, svg, html as t, watch as w, watch };
//# sourceMappingURL=index.mjs.map

/**
 * A queue of expressions to run as soon as an async slot opens up.
 */
const queueMarker = Symbol();
let queueStack = [];
/**
 * A stack of functions to run on the next tick.
 */
let nextTicks = [];
let cleanupCollector = null;
/**
 * Adds the ability to listen to the next tick.
 * @param  {CallableFunction} fn?
 * @returns Promise
 */
function nextTick(fn) {
    return !queueStack.length
        ? Promise.resolve(fn?.())
        : new Promise((resolve) => nextTicks.push(() => {
            fn?.();
            resolve();
        }));
}
function isTpl(template) {
    return typeof template === 'function' && !!template.isT;
}
function isO(obj) {
    return obj !== null && typeof obj === 'object';
}
function isR(obj) {
    return isO(obj) && '$on' in obj;
}
function isChunk(chunk) {
    return isO(chunk) && 'ref' in chunk;
}
/**
 * Queue an item to execute after all synchronous functions have been run. This
 * is used for `w()` to ensure multiple dependency mutations tracked on the
 * same expression do not result in multiple calls.
 * @param  {CallableFunction} fn
 * @returns PropertyObserver
 */
function queue(fn) {
    const queued = fn;
    return (newValue, oldValue) => {
        if (!queued[queueMarker]) {
            queued[queueMarker] = true;
            queued._n = newValue;
            queued._o = oldValue;
            if (!queueStack.length) {
                queueMicrotask(executeQueue);
            }
            queueStack.push(queued);
        }
    };
}
function executeQueue() {
    const queue = queueStack;
    queueStack = [];
    const ticks = nextTicks;
    nextTicks = [];
    for (let i = 0; i < queue.length; i++) {
        const fn = queue[i];
        const newValue = fn._n;
        const oldValue = fn._o;
        fn._n = undefined;
        fn._o = undefined;
        fn[queueMarker] = false;
        fn(newValue, oldValue);
    }
    for (let i = 0; i < ticks.length; i++)
        ticks[i]();
    if (queueStack.length) {
        queueMicrotask(executeQueue);
    }
}
function swapCleanupCollector(collector) {
    const previous = cleanupCollector;
    cleanupCollector = collector;
    return previous;
}
function registerCleanup(fn) {
    cleanupCollector?.push(fn);
}
function onCleanup(fn) {
    const collector = cleanupCollector;
    if (!collector)
        throw Error('onCleanup needs component');
    let active = 1;
    const dispose = () => active-- && (collector.splice(collector.indexOf(dispose), 1), fn());
    return collector.push(dispose), dispose;
}

function setAttr(node, attrName, value) {
    if (attrName === '.innerhtml')
        attrName = '.innerHTML';
    const isIDL = (attrName === 'value' && 'value' in node) ||
        attrName === 'checked' ||
        (attrName[0] === '.' && (attrName = attrName.slice(1)));
    if (isIDL) {
        // @ts-ignore:next-line
        node[attrName] = value;
        if (node.getAttribute(attrName) != value)
            value = false;
    }
    value !== false
        ? node.setAttribute(attrName, value)
        : node.removeAttribute(attrName);
}

const expressionPool = [];
const expressionObservers = [];
const expressionObserverAttrs = [];
const freeExpressionPointers = [];
let cursor = 0;
function createExpressionBlock(len) {
    const bucket = freeExpressionPointers[len];
    const pointer = bucket?.length ? bucket.pop() : cursor;
    expressionPool[pointer] = len;
    if (pointer === cursor)
        cursor += len + 1;
    return pointer;
}
function writeExpressions(expSlots, pointer, offset = 0) {
    const len = expressionPool[pointer];
    for (let i = 1; i <= len; i++) {
        const nextValue = expSlots[offset + i - 1];
        const target = pointer + i;
        if (Object.is(expressionPool[target], nextValue))
            continue;
        expressionPool[target] = nextValue;
        const observer = expressionObservers[target];
        if (!observer)
            continue;
        const attr = expressionObserverAttrs[target];
        if (attr !== undefined)
            setAttr(observer, attr, nextValue);
        else if (typeof observer === 'function')
            observer(nextValue);
        else
            observer.data = nextValue || nextValue === 0 ? nextValue : '';
    }
}
function onExpressionUpdate(pointer, observer, attrName) {
    expressionObservers[pointer] = observer;
    expressionObserverAttrs[pointer] = attrName;
}
function releaseExpressions(pointer) {
    const len = expressionPool[pointer];
    if (len === undefined)
        return;
    for (let i = 0; i <= len; i++) {
        expressionPool[pointer + i] = undefined;
        expressionObservers[pointer + i] = undefined;
        expressionObserverAttrs[pointer + i] = undefined;
    }
    (freeExpressionPointers[len] ??= []).push(pointer);
}

/**
 * A registry of reactive objects to their unique numeric index which serves as
 * an unique identifier.
 */
const ids = new WeakMap();
const computedIds = [];
/**
 * A registry of reactive objects to their property observers.
 */
const listeners = [];
/**
 * Gets the unique id of a given target.
 * @param target - The object to get the id of.
 * @returns
 */
const getId = (target) => ids.get(target);
/**
 * An index counter for the reactive objects.
 */
let index = -1;
/**
 * An index counter to identify watchers.
 */
let watchIndex = 0;
/**
 * The current key being tracked.
 */
let trackKey = 0;
/**
 * Array methods that modify the array.
 */
/**
 * A registry of dependencies for each tracked key.
 */
const trackedDependencies = [];
/**
 * A registry of dependencies that are being watched by a given watcher.
 */
const watchedDependencies = [];
const dependencyPool = [];
const arrayMutationWrappers = [];
const arrayMutations = {
    push: 1,
    pop: 1,
    shift: 1,
    unshift: 1,
    splice: 1,
    sort: 1,
    copyWithin: 1,
    fill: 1,
    reverse: 1,
};
/**
 * A map of child ids to their parents (a child can have multiple parents).
 */
const parents = [];
function reactive(data) {
    if (typeof data === 'function') {
        const state = reactive({
            value: undefined,
        });
        computedIds[getId(state)] = true;
        watch(data, (value) => (state.value = value));
        return state;
    }
    // The data is already a reactive object, so return it.
    if (isR(data))
        return data;
    // Only valid objects can be reactive.
    if (!isO(data))
        throw Error('Expected object');
    // Create a new slot in the listeners registry and then store the relationship
    // of this object to its index.
    const id = ++index;
    listeners[id] = {};
    // Create the actual reactive proxy.
    const proxy = new Proxy(data, proxyHandler);
    // let the ids know about the index
    ids.set(data, id).set(proxy, id);
    return proxy;
}
/**
 *
 * @param parentId - The id of the parent object.
 * @param property - The property of the parent object.
 * @param value - The value of the property.
 * @returns
 */
function trackArray(id, key, target, value) {
    if (typeof value === 'function' &&
        arrayMutations[key]) {
        let wrappers = arrayMutationWrappers[id];
        if (!wrappers)
            wrappers = arrayMutationWrappers[id] = {};
        let wrapper = wrappers[key];
        if (!wrapper) {
            wrapper = (...args) => {
                const result = Reflect.apply(value, target, args);
                emitParents(id);
                return result;
            };
            wrappers[key] = wrapper;
        }
        return wrapper;
    }
    if (isComputed(value))
        return readComputed(value, id, key);
    if (key !== 'length' && typeof value !== 'function') {
        track(id, key);
    }
    return value;
}
const proxyHandler = {
    has(target, key) {
        if (key in api)
            return true;
        track(getId(target), key);
        return key in target;
    },
    get(target, key, receiver) {
        const id = getId(target);
        if (key in api)
            return api[key];
        const result = Reflect.get(target, key, receiver);
        let child;
        if (isO(result) && !isR(result)) {
            child = createChild(result, id, key);
            target[key] = child;
        }
        const value = child ?? result;
        if (Array.isArray(target))
            return trackArray(id, key, target, value);
        if (isComputed(value))
            return readComputed(value, id, key);
        track(id, key);
        return value;
    },
    set(target, key, value, receiver) {
        const id = getId(target);
        const isNewProperty = !(key in target);
        const newReactive = isO(value) && !isR(value) ? createChild(value, id, key) : null;
        const oldValue = target[key];
        const newValue = newReactive ?? value;
        if (isR(newValue) && computedIds[getId(newValue)]) {
            linkParent(getId(newValue), id, key);
        }
        const didSucceed = Reflect.set(target, key, newValue, receiver);
        if (oldValue !== newValue && isR(oldValue) && isR(newValue)) {
            const oldParents = parents[getId(oldValue)];
            if (oldParents) {
                let index = -1;
                for (let i = 0; i < oldParents.length; i++) {
                    const [parent, property] = oldParents[i];
                    if (parent == id && property == key) {
                        index = i;
                        break;
                    }
                }
                if (index > -1)
                    oldParents.splice(index, 1);
            }
            linkParent(getId(newValue), id, key);
        }
        emit(id, key, value, oldValue, isNewProperty || (key === 'value' && computedIds[id]));
        if (Array.isArray(target) && key === 'length') {
            emitParents(id);
        }
        return didSucceed;
    },
};
/**
 *
 * @param child - Creates a child relationship
 * @param parent
 * @param key
 * @returns
 */
function createChild(child, parentId, key) {
    const r = reactive(child);
    linkParent(getId(child), parentId, key);
    return r;
}
function isComputed(value) {
    return isR(value) && computedIds[getId(value)];
}
function readComputed(value, parentId, key) {
    const computedId = getId(value);
    track(parentId, key);
    linkParent(computedId, parentId, key);
    track(computedId, 'value');
    return value.value;
}
function linkParent(childId, parentId, key) {
    const entries = parents[childId];
    if (entries) {
        for (let i = 0; i < entries.length; i++) {
            const [parent, property] = entries[i];
            if (parent === parentId && property === key)
                return;
        }
    }
    else {
        parents[childId] = [];
    }
    parents[childId].push([parentId, key]);
}
/**
 *
 * @param id - The reactive id that changed.
 * @param key - The property that changed.
 * @param newValue - The new value of the property.
 * @param oldValue - The old value of the property.
 */
function emit(id, key, newValue, oldValue, notifyParents) {
    const targetListeners = listeners[id];
    const propertyListeners = targetListeners[key];
    if (propertyListeners) {
        if (Array.isArray(propertyListeners)) {
            for (let i = 0; i < propertyListeners.length; i++) {
                propertyListeners[i](newValue, oldValue);
            }
        }
        else {
            propertyListeners(newValue, oldValue);
        }
    }
    if (notifyParents) {
        emitParents(id);
    }
}
function emitParents(id) {
    const parentEntries = parents[id];
    if (!parentEntries)
        return;
    for (let i = 0; i < parentEntries.length; i++) {
        const [parentId, property] = parentEntries[i];
        emit(parentId, property);
    }
}
function reactiveOn(property, callback) {
    addListener(listeners[getId(this)], property, callback);
}
function reactiveOff(property, callback) {
    removeListener(listeners[getId(this)], property, callback);
}
/**
 * The public reactive API for a reactive object.
 */
const api = {
    $on: reactiveOn,
    $off: reactiveOff,
};
/**
 * Track a reactive property as a dependency.
 * @param target
 * @param key
 */
function track(id, property) {
    if (!trackKey)
        return;
    trackedDependencies[trackKey].push(id, property);
}
/**
 * Begin tracking reactive dependencies.
 */
function startTracking() {
    trackedDependencies[++trackKey] = dependencyPool.pop() ?? [];
}
/**
 * Stop tracking reactive dependencies and register a callback for when any of
 * the tracked dependencies change.
 * @param callback - A function to re-run when dependencies change.
 */
function stopTracking(watchKey, callback) {
    const key = trackKey--;
    const deps = trackedDependencies[key];
    const previousDeps = watchedDependencies[watchKey];
    const previousLength = previousDeps?.length;
    if (previousLength && previousLength === deps.length) {
        let matched = true;
        for (let i = 0; i < previousLength; i++) {
            if (previousDeps[i] === deps[i])
                continue;
            matched = false;
            break;
        }
        if (matched) {
            watchedDependencies[watchKey] = previousDeps;
            deps.length = 0;
            dependencyPool.push(deps);
            trackedDependencies[key] = undefined;
            return;
        }
    }
    flushListeners(previousDeps, callback);
    for (let i = 0; i < deps.length; i += 2) {
        addListener(listeners[deps[i]], deps[i + 1], callback);
    }
    watchedDependencies[watchKey] = deps;
    trackedDependencies[key] = undefined;
}
/**
 * Removes a callback from the listeners registry for a given set of
 * dependencies.
 * @param deps - The dependencies to flush.
 * @param callback - The callback to remove.
 */
function flushListeners(deps, callback) {
    if (!deps)
        return;
    for (let i = 0; i < deps.length; i += 2) {
        removeListener(listeners[deps[i]], deps[i + 1], callback);
    }
    deps.length = 0;
    dependencyPool.push(deps);
}
function addListener(targetListeners, key, callback) {
    const slot = targetListeners[key];
    if (!slot) {
        targetListeners[key] = callback;
        return;
    }
    if (Array.isArray(slot)) {
        if (!slot.includes(callback))
            slot.push(callback);
        return;
    }
    if (slot !== callback)
        targetListeners[key] = [slot, callback];
}
function removeListener(targetListeners, key, callback) {
    const slot = targetListeners[key];
    if (!slot)
        return;
    if (Array.isArray(slot)) {
        const index = slot.indexOf(callback);
        if (index < 0)
            return;
        if (slot.length === 2) {
            targetListeners[key] = slot[index ? 0 : 1];
            return;
        }
        slot.splice(index, 1);
        return;
    }
    if (slot === callback) {
        delete targetListeners[key];
    }
}
function watch(effect, afterEffect) {
    const watchKey = ++watchIndex;
    const isPointer = typeof effect === 'number';
    let rerun = queue(runEffect);
    function runEffect() {
        startTracking();
        const effectValue = isPointer
            ? expressionPool[effect]()
            : effect();
        stopTracking(watchKey, rerun);
        return afterEffect ? afterEffect(effectValue) : effectValue;
    }
    const stop = () => {
        flushListeners(watchedDependencies[watchKey], rerun);
        watchedDependencies[watchKey] = undefined;
        if (isPointer)
            onExpressionUpdate(effect);
        rerun = null;
    };
    if (!isPointer)
        registerCleanup(stop);
    if (isPointer)
        onExpressionUpdate(effect, runEffect);
    return [runEffect(), stop];
}

const AsyncFunction = (async () => { }).constructor;
let asyncComponentInstaller = null;
function setComponentKey(key) {
    this.k = key;
    return this;
}
const propsProxyHandler = {
    get(target, key) {
        return target[0]?.[key];
    },
    has(target, key) {
        return key in (target[0] || {});
    },
    ownKeys(target) {
        return Reflect.ownKeys(target[0] || {});
    },
    getOwnPropertyDescriptor(target, key) {
        const source = target[0];
        return source && {
            configurable: true,
            enumerable: true,
            writable: true,
            value: source[key],
        };
    },
    set(target, key, value) {
        return !!target[0] && Reflect.set(target[0], key, value);
    },
};
const narrowedPropsHandler = {
    get(target, key) {
        return target.k.includes(key)
            ? target.s[key]
            : undefined;
    },
    set(target, key, value) {
        if (!target.k.includes(key))
            return false;
        return Reflect.set(target.s, key, value);
    },
};
function pick(source, ...keys) {
    return keys.length
        ? new Proxy({
            k: keys,
            s: source,
        }, narrowedPropsHandler)
        : source;
}
function component(factory, options) {
    if (options || factory.constructor === AsyncFunction) {
        if (!asyncComponentInstaller) {
            throw Error('Async runtime missing.');
        }
        return asyncComponentInstaller(factory, options);
    }
    return ((input, events) => ({
        h: factory,
        k: undefined,
        p: input,
        e: events,
        key: setComponentKey,
    }));
}
function installAsyncComponentInstaller(installer) {
    asyncComponentInstaller = installer;
}
function isCmp(value) {
    return !!value && typeof value === 'object' && 'h' in value;
}
function createPropsProxy(source, factory, events) {
    const box = reactive({ 0: source, 1: factory, 2: events });
    const emit = ((event, payload) => {
        const handler = box[2]?.[event];
        if (typeof handler === 'function')
            handler(payload);
    });
    return [
        new Proxy(box, propsProxyHandler),
        emit,
        box,
    ];
}

let hydrationCaptureProvider = null;
function installHydrationCaptureProvider(provider) {
    hydrationCaptureProvider = provider;
}
function createHydrationCapture() {
    return {
        hooks: new WeakMap(),
    };
}
function getHydrationCapture() {
    return hydrationCaptureProvider?.() ?? null;
}
function registerHydrationHook(chunk, hook) {
    const capture = getHydrationCapture();
    if (!capture)
        return;
    const hooks = capture.hooks.get(chunk);
    if (hooks) {
        hooks.push(hook);
    }
    else {
        capture.hooks.set(chunk, [hook]);
    }
}
function adoptCapturedChunk(capture, chunk, map, visited = new WeakSet()) {
    if (visited.has(chunk))
        return;
    visited.add(chunk);
    const ref = chunk.ref;
    if (ref.f)
        ref.f = map.get(ref.f) ?? ref.f;
    if (ref.l)
        ref.l = map.get(ref.l) ?? ref.l;
    capture.hooks.get(chunk)?.forEach((hook) => hook(map, visited));
}

export { watch as a, isCmp as b, createExpressionBlock as c, isChunk as d, expressionPool as e, adoptCapturedChunk as f, getHydrationCapture as g, createPropsProxy as h, isTpl as i, releaseExpressions as j, swapCleanupCollector as k, component as l, onCleanup as m, nextTick as n, onExpressionUpdate as o, pick as p, reactive as q, registerHydrationHook as r, setAttr as s, createHydrationCapture as t, installAsyncComponentInstaller as u, installHydrationCaptureProvider as v, writeExpressions as w };
//# sourceMappingURL=internal-DchK7S7v.mjs.map

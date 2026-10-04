interface ArrowTemplate {
    (parent: ParentNode): ParentNode;
    (): DocumentFragment;
    isT: boolean;
    key: (key: ArrowTemplateKey) => ArrowTemplate;
    id: (id: ArrowTemplateId) => ArrowTemplate;
    _c: () => Chunk;
    _k: ArrowTemplateKey;
    _i?: ArrowTemplateId;
}
type ArrowTemplateKey = string | number | undefined;
type ArrowTemplateId = string | number | undefined;
type ArrowRenderable = string | number | boolean | null | undefined | ComponentCall | ArrowTemplate | Array<string | number | boolean | ComponentCall | ArrowTemplate>;
type ParentNode = Node | DocumentFragment;
type ArrowFunction = (...args: unknown[]) => ArrowRenderable;
type ArrowExpression = ArrowRenderable | ArrowFunction | EventListener | ((evt: InputEvent) => void);
interface Chunk {
    paths: [number[], string[]];
    dom: DocumentFragment;
    ref: DOMRef;
    _t: ArrowTemplate;
    k?: ArrowTemplateKey;
    i?: ArrowTemplateId;
    e: number;
    g: string;
    b: boolean;
    r: boolean;
    st: boolean;
    bkn?: Chunk;
    v?: Array<[Element, string]> | null;
    u?: Array<() => void> | null;
    s?: ReturnType<typeof createPropsProxy>[2];
    mk?: number;
    next?: Chunk;
}
interface DOMRef {
    f: ChildNode | null;
    l: ChildNode | null;
}
declare function html(strings: TemplateStringsArray | string[], ...expSlots: ArrowExpression[]): ArrowTemplate;
declare function svg(strings: TemplateStringsArray | string[], ...expSlots: ArrowExpression[]): ArrowTemplate;

/**
 * The target of a reactive object.
 */
type ReactiveTarget = Record<PropertyKey, unknown> | unknown[];
interface ReactiveAPI<T> {
    /**
     * Adds an observer to a given property.
     * @param p - The property to watch.
     * @param c - The callback to call when the property changes.
     * @returns
     */
    $on: <P extends keyof T>(p: P, c: PropertyObserver<T[P]>) => void;
    /**
     * Removes an observer from a given property.
     * @param p - The property to stop watching.
     * @param c - The callback to stop calling when the property changes.
     * @returns
     */
    $off: <P extends keyof T>(p: P, c: PropertyObserver<T[P]>) => void;
}
/**
 * A reactive object is a proxy of an original object.
 */
interface Computed<T> extends Readonly<Reactive<{
    value: T;
}>> {
}
type ReactiveValue<T> = T extends Computed<infer TValue> ? TValue : T extends ReactiveTarget ? Reactive<T> | T : T;
type Reactive<T extends ReactiveTarget> = {
    [P in keyof T]: ReactiveValue<T[P]>;
} & ReactiveAPI<T>;
/**
 * A callback used to observe a property changes on a reactive object.
 */
interface PropertyObserver<T> {
    (newValue?: T, oldValue?: T): void;
}
/**
 * A reactive object is a proxy of the original object that allows for
 * reactive dependency watching. It is created by calling `reactive()` and
 * should be used to store reactive data in your app and components.
 *
 * @param data - The data to make reactive, typically a plain object.
 * @returns A reactive proxy of the original data.
 */
declare function reactive<T>(effect: () => T): Computed<T>;
declare function reactive<T extends ReactiveTarget>(data: T): Reactive<T>;
/**
 * Calls a function and watches it for changes.
 * @param fn - A function to watch.
 * @param after - A function to call after the watched function with the result.
 */
declare function watch<A extends (arg: ArrowRenderable) => unknown>(pointer: number, afterEffect: A): [returnValue: ReturnType<A>, stop: () => void];
declare function watch<F extends (...args: unknown[]) => unknown>(effect: F): [returnValue: ReturnType<F>, stop: () => void];
declare function watch<F extends (...args: unknown[]) => unknown, A extends (arg: ReturnType<F>) => unknown>(effect: F, afterEffect: A): [returnValue: ReturnType<A>, stop: () => void];

type Props<T extends ReactiveTarget> = {
    [P in keyof T]: T[P] extends ReactiveTarget ? Props<T[P]> | T[P] : T[P];
};
type EventMap = Record<string, unknown>;
type Events<T extends EventMap> = {
    [K in keyof T]?: (payload: T[K]) => void;
};
type Emit<T extends EventMap> = <K extends keyof T>(event: K, payload: T[K]) => void;
type ComponentFactory = (props?: Props<ReactiveTarget>, emit?: Emit<EventMap>) => ArrowTemplate;
interface AsyncComponentOptions<TProps extends ReactiveTarget, TValue, TEvents extends EventMap = EventMap, TSnapshot = TValue> {
    fallback?: unknown;
    onError?: (error: unknown, props: Props<TProps>, emit: Emit<TEvents>) => unknown;
    render?: (value: TValue, props: Props<TProps>, emit: Emit<TEvents>) => unknown;
    serialize?: (value: TValue, props: Props<TProps>, emit: Emit<TEvents>) => TSnapshot;
    deserialize?: (snapshot: TSnapshot, props: Props<TProps>) => TValue;
    idPrefix?: string;
}
interface ComponentCall {
    h: ComponentFactory;
    p: Props<ReactiveTarget> | undefined;
    e: Events<EventMap> | undefined;
    k: ArrowTemplateKey;
    key: (key: ArrowTemplateKey) => ComponentCall;
}
interface Component<TEvents extends EventMap = EventMap> {
    (props?: undefined, events?: Events<TEvents>): ComponentCall;
}
interface ComponentWithProps<T extends ReactiveTarget, TEvents extends EventMap = EventMap> {
    <S extends T>(props: S, events?: Events<TEvents>): ComponentCall;
}
type SourceBox = Reactive<{
    0: Props<ReactiveTarget> | undefined;
    1: ComponentFactory;
    2: Events<EventMap> | undefined;
}>;
declare function pick<T extends object, K extends keyof T>(source: T, ...keys: K[]): Pick<T, K>;
declare function pick<T extends object>(source: T): T;
declare function component(factory: () => ArrowTemplate): Component;
declare function component<TEvents extends EventMap>(factory: (props: undefined, emit: Emit<TEvents>) => ArrowTemplate): Component<TEvents>;
declare function component<T extends ReactiveTarget>(factory: (props: Props<T>) => ArrowTemplate): ComponentWithProps<T>;
declare function component<T extends ReactiveTarget, TEvents extends EventMap>(factory: (props: Props<T>, emit: Emit<TEvents>) => ArrowTemplate): ComponentWithProps<T, TEvents>;
declare function component<TValue, TEvents extends EventMap, TSnapshot = TValue>(factory: (() => Promise<TValue> | TValue) | ((props: undefined, emit: Emit<TEvents>) => Promise<TValue> | TValue), options?: AsyncComponentOptions<ReactiveTarget, TValue, TEvents, TSnapshot>): Component;
declare function component<T extends ReactiveTarget, TValue, TEvents extends EventMap, TSnapshot = TValue>(factory: ((props: Props<T>) => Promise<TValue> | TValue) | ((props: Props<T>, emit: Emit<TEvents>) => Promise<TValue> | TValue), options?: AsyncComponentOptions<T, TValue, TEvents, TSnapshot>): ComponentWithProps<T, TEvents>;
declare function createPropsProxy(source: Props<ReactiveTarget> | undefined, factory: ComponentFactory, events?: Events<EventMap>): [Props<ReactiveTarget>, Emit<EventMap>, SourceBox];

/**
 * Adds the ability to listen to the next tick.
 * @param  {CallableFunction} fn?
 * @returns Promise
 */
declare function nextTick(fn?: CallableFunction): Promise<unknown>;
declare function onCleanup(fn: () => void): () => void | 0;

export { component as c, component, html, nextTick, onCleanup, pick, pick as props, reactive as r, reactive, svg, html as t, watch as w, watch };
export type { ArrowExpression, ArrowRenderable, ArrowTemplate, ArrowTemplateKey, AsyncComponentOptions, Component, ComponentCall, ComponentWithProps, Computed, Emit, EventMap, Events, ParentNode, PropertyObserver, Props, Reactive, ReactiveTarget };

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
type ParentNode = Node | DocumentFragment;
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

type Props<T extends ReactiveTarget> = {
    [P in keyof T]: T[P] extends ReactiveTarget ? Props<T[P]> | T[P] : T[P];
};
type EventMap = Record<string, unknown>;
type Events<T extends EventMap> = {
    [K in keyof T]?: (payload: T[K]) => void;
};
type Emit<T extends EventMap> = <K extends keyof T>(event: K, payload: T[K]) => void;
type AsyncFactory<T extends ReactiveTarget, TValue, TEvents extends EventMap> = (() => Promise<TValue> | TValue) | ((props: Props<T>) => Promise<TValue> | TValue) | ((props: Props<T>, emit: Emit<TEvents>) => Promise<TValue> | TValue) | ((props: undefined, emit: Emit<TEvents>) => Promise<TValue> | TValue);
type ComponentFactory = (props?: Props<ReactiveTarget>, emit?: Emit<EventMap>) => ArrowTemplate;
interface AsyncComponentOptions<TProps extends ReactiveTarget, TValue, TEvents extends EventMap = EventMap, TSnapshot = TValue> {
    fallback?: unknown;
    onError?: (error: unknown, props: Props<TProps>, emit: Emit<TEvents>) => unknown;
    render?: (value: TValue, props: Props<TProps>, emit: Emit<TEvents>) => unknown;
    serialize?: (value: TValue, props: Props<TProps>, emit: Emit<TEvents>) => TSnapshot;
    deserialize?: (snapshot: TSnapshot, props: Props<TProps>) => TValue;
    idPrefix?: string;
}
type AsyncComponentInstaller = <TProps extends ReactiveTarget, TValue, TEvents extends EventMap = EventMap, TSnapshot = TValue>(factory: AsyncFactory<TProps, TValue, TEvents>, options?: AsyncComponentOptions<TProps, TValue, TEvents, TSnapshot>) => Component<TEvents> | ComponentWithProps<TProps, TEvents>;
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
declare function installAsyncComponentInstaller(installer: AsyncComponentInstaller | null): void;
declare function createPropsProxy(source: Props<ReactiveTarget> | undefined, factory: ComponentFactory, events?: Events<EventMap>): [Props<ReactiveTarget>, Emit<EventMap>, SourceBox];

type NodeMap = WeakMap<Node, Node>;
type HydrationHook = (map: NodeMap, visited: WeakSet<Chunk>) => void;
interface HydrationCapture {
    hooks: WeakMap<Chunk, HydrationHook[]>;
}
type HydrationCaptureProvider = () => HydrationCapture | null;
declare function installHydrationCaptureProvider(provider: HydrationCaptureProvider | null): void;
declare function createHydrationCapture(): HydrationCapture;
declare function adoptCapturedChunk(capture: HydrationCapture, chunk: Chunk, map: NodeMap, visited?: WeakSet<Chunk>): void;

export { adoptCapturedChunk, createHydrationCapture, installAsyncComponentInstaller, installHydrationCaptureProvider };
export type { ArrowTemplate, HydrationCapture, ParentNode };

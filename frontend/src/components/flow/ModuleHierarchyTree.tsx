import type { ModuleTreeNode } from "@/lib/verilog";

function ModuleTreeItem({
  node,
  selectedTop,
  suggestedTops,
  onSelect,
  depth,
}: {
  node: ModuleTreeNode;
  selectedTop: string;
  suggestedTops: string[];
  onSelect?: (name: string) => void;
  depth: number;
}) {
  const isTop = node.name === selectedTop;
  const isSuggested = suggestedTops.includes(node.name);
  const hasChildren = node.children.length > 0;

  return (
    <li
      className={[
        "hierarchy-tree-item",
        hasChildren ? "has-children" : "is-leaf",
        isTop ? "is-top" : "",
        isSuggested && !isTop ? "is-suggested" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      role="treeitem"
      aria-selected={isTop}
      aria-expanded={hasChildren ? true : undefined}
    >
      <button
        type="button"
        className="hierarchy-tree-node"
        onClick={() => onSelect?.(node.name)}
        title={onSelect ? `Select ${node.name} as top module` : undefined}
      >
        <span className="hierarchy-tree-glyph" aria-hidden="true">
          {hasChildren ? (
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
              <rect
                x="2.5"
                y="2.5"
                width="11"
                height="11"
                rx="2"
                stroke="currentColor"
                strokeWidth="1.4"
              />
              <path
                d="M5 8h6M8 5v6"
                stroke="currentColor"
                strokeWidth="1.4"
                strokeLinecap="round"
              />
            </svg>
          ) : (
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
              <rect
                x="3"
                y="3"
                width="10"
                height="10"
                rx="2"
                stroke="currentColor"
                strokeWidth="1.4"
              />
            </svg>
          )}
        </span>
        <span className="hierarchy-tree-name">{node.name}</span>
        {node.file ? <span className="hierarchy-tree-file">{node.file}</span> : null}
        {isTop ? <span className="hierarchy-tree-badge">top</span> : null}
        {!isTop && isSuggested && depth === 0 ? (
          <span className="hierarchy-tree-badge is-soft">suggested</span>
        ) : null}
      </button>
      {hasChildren ? (
        <ul className="hierarchy-tree-children" role="group">
          {node.children.map((child, index) => (
            <ModuleTreeItem
              key={`${child.name}:${child.file}:${index}`}
              node={child}
              selectedTop={selectedTop}
              suggestedTops={suggestedTops}
              onSelect={onSelect}
              depth={depth + 1}
            />
          ))}
        </ul>
      ) : null}
    </li>
  );
}

export function ModuleHierarchyTree({
  nodes,
  selectedTop,
  suggestedTops,
  onSelect,
}: {
  nodes: ModuleTreeNode[];
  selectedTop: string;
  suggestedTops: string[];
  onSelect?: (name: string) => void;
}) {
  if (!nodes.length) {
    return <p className="meta hierarchy-tree-empty">No hierarchy detected.</p>;
  }

  return (
    <div className="hierarchy-tree-wrap">
      <ul className="hierarchy-tree" role="tree" aria-label="Module hierarchy">
        {nodes.map((node, index) => (
          <ModuleTreeItem
            key={`${node.name}:${node.file}:${index}`}
            node={node}
            selectedTop={selectedTop}
            suggestedTops={suggestedTops}
            onSelect={onSelect}
            depth={0}
          />
        ))}
      </ul>
    </div>
  );
}

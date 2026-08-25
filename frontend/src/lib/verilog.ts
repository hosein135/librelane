/** Client-side Verilog checks: modules, hierarchy, and top-module detection. */

const IDENT = "[A-Za-z_][A-Za-z0-9_$]*";
const MODULE_DECL_RE = new RegExp(`\\bmodule\\s+(${IDENT})\\b`, "g");
const ENDMODULE_RE = /\bendmodule\b/g;

const KEYWORDS = new Set([
  "module",
  "endmodule",
  "macromodule",
  "input",
  "output",
  "inout",
  "wire",
  "reg",
  "logic",
  "assign",
  "always",
  "always_ff",
  "always_comb",
  "always_latch",
  "begin",
  "end",
  "if",
  "else",
  "for",
  "while",
  "case",
  "casex",
  "casez",
  "endcase",
  "default",
  "parameter",
  "localparam",
  "generate",
  "endgenerate",
  "genvar",
  "function",
  "endfunction",
  "task",
  "endtask",
  "integer",
  "real",
  "posedge",
  "negedge",
  "or",
  "and",
  "not",
  "xor",
  "nand",
  "nor",
  "xnor",
  "supply0",
  "supply1",
  "tri",
  "tri0",
  "tri1",
  "trireg",
  "signed",
  "unsigned",
  "initial",
  "forever",
  "repeat",
  "wait",
  "disable",
  "force",
  "release",
  "fork",
  "join",
  "specify",
  "endspecify",
  "primitive",
  "endprimitive",
  "table",
  "endtable",
  "config",
  "endconfig",
  "library",
  "include",
  "define",
  "ifdef",
  "ifndef",
  "endif",
  "elsif",
  "timescale",
  "default_nettype",
  "celldefine",
  "endcelldefine",
  "typedef",
  "struct",
  "enum",
  "union",
  "package",
  "endpackage",
  "import",
  "export",
  "interface",
  "endinterface",
  "modport",
  "assert",
  "assume",
  "cover",
  "property",
  "sequence",
  "unique",
  "priority",
  "automatic",
  "static",
  "const",
  "var",
  "void",
  "return",
  "break",
  "continue",
  "do",
  "foreach",
  "alias",
  "bind",
  "checker",
  "endchecker",
  "clocking",
  "endclocking",
  "covergroup",
  "endgroup",
  "program",
  "endprogram",
  "rand",
  "randc",
  "with",
  "inside",
  "dist",
  "soft",
  "solve",
  "before",
  "super",
  "this",
  "null",
  "new",
  "extends",
  "virtual",
  "pure",
  "extern",
  "local",
  "protected",
  "ref",
  "chandle",
  "string",
  "event",
  "time",
  "shortint",
  "longint",
  "byte",
  "bit",
  "int",
]);

const ALLOWED_EXT = new Set([".v", ".sv"]);
const MAX_FILES = 50;
const MAX_FILE_BYTES = 2 * 1024 * 1024;
const MAX_TOTAL_BYTES = 10 * 1024 * 1024;

export type VerilogFileInput = {
  name: string;
  content: string;
  size: number;
};

export type ModuleInfo = {
  name: string;
  file: string;
  children: string[];
};

export type ModuleTreeNode = {
  name: string;
  file: string;
  children: ModuleTreeNode[];
};

export type VerilogAnalysis = {
  ok: boolean;
  errors: string[];
  warnings: string[];
  modules: ModuleInfo[];
  moduleNames: string[];
  tree: ModuleTreeNode[];
  suggestedTops: string[];
  autoTop: string | null;
};

function stripComments(source: string): string {
  const withoutBlock = source.replace(/\/\*[\s\S]*?\*\//g, " ");
  return withoutBlock.replace(/\/\/.*$/gm, " ");
}

function basename(path: string): string {
  const parts = path.replace(/\\/g, "/").split("/");
  return parts[parts.length - 1] || path;
}

function extensionOf(name: string): string {
  const base = basename(name).toLowerCase();
  const i = base.lastIndexOf(".");
  return i >= 0 ? base.slice(i) : "";
}

function sanitizeUploadName(name: string): string | null {
  const base = basename(name).trim();
  if (!base || base === "." || base === "..") return null;
  if (base.includes("..") || /[\\/]/.test(base)) return null;
  if (!ALLOWED_EXT.has(extensionOf(base))) return null;
  if (!/^[A-Za-z0-9._+-]+$/.test(base)) return null;
  return base;
}

/** Match `Type [#(...)] instance (` while ignoring keywords. */
const INST_RE = new RegExp(
  `\\b(${IDENT})\\s+(?:#\\s*\\([^;]*?\\)\\s*)?(${IDENT})\\s*\\(`,
  "g",
);

function findModuleRegions(clean: string): { name: string; body: string }[] {
  const regions: { name: string; body: string }[] = [];
  MODULE_DECL_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = MODULE_DECL_RE.exec(clean)) !== null) {
    const name = match[1];
    const bodyStart = match.index + match[0].length;
    ENDMODULE_RE.lastIndex = bodyStart;
    const end = ENDMODULE_RE.exec(clean);
    if (!end) {
      regions.push({ name, body: clean.slice(bodyStart) });
      break;
    }
    regions.push({ name, body: clean.slice(bodyStart, end.index) });
    MODULE_DECL_RE.lastIndex = end.index + end[0].length;
  }
  return regions;
}

function findInstantiations(body: string, knownModules: Set<string>): string[] {
  const found = new Set<string>();
  INST_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = INST_RE.exec(body)) !== null) {
    const type = match[1];
    const inst = match[2];
    if (KEYWORDS.has(type) || KEYWORDS.has(inst)) continue;
    if (!knownModules.has(type)) continue;
    found.add(type);
  }
  return [...found];
}

function buildForest(
  modules: ModuleInfo[],
  roots: string[],
): ModuleTreeNode[] {
  const byName = new Map(modules.map((m) => [m.name, m]));
  const visiting = new Set<string>();

  function walk(name: string): ModuleTreeNode {
    const info = byName.get(name);
    if (!info) {
      return { name, file: "", children: [] };
    }
    if (visiting.has(name)) {
      return { name, file: info.file, children: [] };
    }
    visiting.add(name);
    const children = info.children
      .filter((c) => byName.has(c))
      .map((c) => walk(c));
    visiting.delete(name);
    return { name, file: info.file, children };
  }

  return roots.map(walk);
}

export function analyzeVerilogFiles(files: VerilogFileInput[]): VerilogAnalysis {
  const errors: string[] = [];
  const warnings: string[] = [];

  if (files.length === 0) {
    return {
      ok: false,
      errors: ["Upload at least one Verilog file (.v or .sv)."],
      warnings: [],
      modules: [],
      moduleNames: [],
      tree: [],
      suggestedTops: [],
      autoTop: null,
    };
  }
  if (files.length > MAX_FILES) {
    errors.push(`Too many files (max ${MAX_FILES}).`);
  }

  let total = 0;
  const seenNames = new Set<string>();
  const normalized: { name: string; content: string }[] = [];

  for (const file of files) {
    const safe = sanitizeUploadName(file.name);
    if (!safe) {
      errors.push(
        `Invalid file name or extension: ${basename(file.name)}. Use .v / .sv and safe characters.`,
      );
      continue;
    }
    if (seenNames.has(safe.toLowerCase())) {
      errors.push(`Duplicate file name: ${safe}`);
      continue;
    }
    seenNames.add(safe.toLowerCase());
    if (file.size <= 0 || !file.content.trim()) {
      errors.push(`${safe} is empty.`);
      continue;
    }
    if (file.size > MAX_FILE_BYTES) {
      errors.push(`${safe} exceeds ${MAX_FILE_BYTES / (1024 * 1024)} MiB.`);
      continue;
    }
    total += file.size;
    if (/[\u0000]/.test(file.content)) {
      errors.push(`${safe} looks binary (contains null bytes).`);
      continue;
    }
    normalized.push({ name: safe, content: file.content });
  }

  if (total > MAX_TOTAL_BYTES) {
    errors.push(`Total upload exceeds ${MAX_TOTAL_BYTES / (1024 * 1024)} MiB.`);
  }

  if (errors.length) {
    return {
      ok: false,
      errors,
      warnings,
      modules: [],
      moduleNames: [],
      tree: [],
      suggestedTops: [],
      autoTop: null,
    };
  }

  const moduleToFile = new Map<string, string>();
  const regionsByFile: { file: string; regions: { name: string; body: string }[] }[] =
    [];

  for (const file of normalized) {
    const clean = stripComments(file.content);
    if (!/\bmodule\b/.test(clean)) {
      errors.push(`${file.name}: no module declaration found.`);
      continue;
    }
    const regions = findModuleRegions(clean);
    if (regions.length === 0) {
      errors.push(`${file.name}: could not parse module declarations.`);
      continue;
    }
    for (const region of regions) {
      if (moduleToFile.has(region.name)) {
        errors.push(
          `Duplicate module ${region.name}: in ${moduleToFile.get(region.name)} and ${file.name}.`,
        );
      } else {
        moduleToFile.set(region.name, file.name);
      }
    }
    regionsByFile.push({ file: file.name, regions });
  }

  if (errors.length) {
    return {
      ok: false,
      errors,
      warnings,
      modules: [],
      moduleNames: [],
      tree: [],
      suggestedTops: [],
      autoTop: null,
    };
  }

  const known = new Set(moduleToFile.keys());
  const childrenMap = new Map<string, string[]>();
  const instantiated = new Set<string>();

  for (const { regions } of regionsByFile) {
    for (const region of regions) {
      const kids = findInstantiations(region.body, known).filter(
        (c) => c !== region.name,
      );
      childrenMap.set(region.name, kids);
      for (const c of kids) instantiated.add(c);
    }
  }

  const modules: ModuleInfo[] = [...moduleToFile.entries()].map(([name, file]) => ({
    name,
    file,
    children: childrenMap.get(name) || [],
  }));
  modules.sort((a, b) => a.name.localeCompare(b.name));

  const suggestedTops = modules
    .map((m) => m.name)
    .filter((name) => !instantiated.has(name));

  if (suggestedTops.length === 0 && modules.length > 0) {
    warnings.push(
      "Every module is instantiated by another (possible cycle). Pick the top module manually.",
    );
  } else if (suggestedTops.length > 1) {
    warnings.push(
      `Multiple top candidates: ${suggestedTops.join(", ")}. Auto-picked the first; you can change it.`,
    );
  }

  // Prefer a root that actually instantiates others when several candidates exist.
  let autoTop: string | null = null;
  if (suggestedTops.length === 1) {
    autoTop = suggestedTops[0];
  } else if (suggestedTops.length > 1) {
    const withKids = suggestedTops.filter(
      (n) => (childrenMap.get(n) || []).length > 0,
    );
    autoTop = (withKids[0] || suggestedTops[0]) ?? null;
  } else if (modules.length === 1) {
    autoTop = modules[0].name;
  }

  const treeRoots =
    suggestedTops.length > 0
      ? suggestedTops
      : autoTop
        ? [autoTop]
        : modules.map((m) => m.name);
  const tree = buildForest(modules, treeRoots);

  return {
    ok: modules.length > 0,
    errors: modules.length === 0 ? ["No modules found in uploaded files."] : [],
    warnings,
    modules,
    moduleNames: modules.map((m) => m.name),
    tree,
    suggestedTops,
    autoTop,
  };
}

export async function readVerilogFiles(fileList: FileList | File[]): Promise<{
  files: VerilogFileInput[];
  errors: string[];
}> {
  const files: VerilogFileInput[] = [];
  const errors: string[] = [];
  const list = Array.from(fileList);

  for (const file of list) {
    try {
      const content = await file.text();
      files.push({ name: file.name, content, size: file.size });
    } catch {
      errors.push(`Could not read ${file.name}.`);
    }
  }
  return { files, errors };
}

export function formatModuleTree(nodes: ModuleTreeNode[], indent = 0): string {
  const lines: string[] = [];
  for (const node of nodes) {
    const pad = "  ".repeat(indent);
    const fileHint = node.file ? ` (${node.file})` : "";
    lines.push(`${pad}${node.name}${fileHint}`);
    if (node.children.length) {
      lines.push(formatModuleTree(node.children, indent + 1));
    }
  }
  return lines.join("\n");
}

export const VERILOG_UPLOAD_LIMITS = {
  maxFiles: MAX_FILES,
  maxFileBytes: MAX_FILE_BYTES,
  maxTotalBytes: MAX_TOTAL_BYTES,
  accept: ".v,.sv",
};

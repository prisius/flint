-- Flint support for Neovim 0.11+: filetype, LSP (diagnostics/lint, hover,
-- definition, references, completion, symbols, formatting), commands.
--
--   require("flint").setup({
--     flint = "/path/to/flint",   -- default: the `flint` script in this repo
--     format_on_save = true,
--     keymaps = true,             -- <leader>cr run, <leader>cb build, <leader>ce show C, <leader>cl lint
--   })
local M = {}

-- editors/nvim/lua/flint/init.lua -> repo root
local function repo_root()
  local src = debug.getinfo(1, "S").source:sub(2)
  return vim.fn.fnamemodify(src, ":p:h:h:h:h:h")
end

local function term(cmd)
  vim.cmd("botright 15split")
  vim.cmd("terminal " .. cmd)
  vim.cmd("startinsert")
end

function M.setup(opts)
  opts = vim.tbl_deep_extend("force", {
    flint = repo_root() .. "/flint",
    format_on_save = true,
    keymaps = true,
  }, opts or {})
  local flint = opts.flint
  if vim.fn.executable(flint) == 0 and vim.fn.executable("flint") == 1 then
    flint = "flint"
  end
  M.flint = flint

  vim.filetype.add({ extension = { fl = "flint" } })

  vim.lsp.config("flint", {
    cmd = { flint, "lsp" },
    filetypes = { "flint" },
    root_markers = { ".git", "README.md" },
  })
  vim.lsp.enable("flint")

  local group = vim.api.nvim_create_augroup("flint", { clear = true })

  if opts.format_on_save then
    vim.api.nvim_create_autocmd("BufWritePre", {
      group = group,
      pattern = "*.fl",
      callback = function(args)
        if #vim.lsp.get_clients({ bufnr = args.buf, name = "flint" }) > 0 then
          vim.lsp.buf.format({ bufnr = args.buf, name = "flint", timeout_ms = 2000 })
        end
      end,
    })
  end

  local function file()
    return vim.fn.shellescape(vim.fn.expand("%:p"))
  end
  local fl = vim.fn.shellescape(flint)
  vim.api.nvim_create_user_command("FlintRun", function(c)
    vim.cmd("silent! write")
    term(fl .. " run " .. file() .. " " .. c.args)
  end, { nargs = "*", desc = "Flint: compile and run current file" })
  vim.api.nvim_create_user_command("FlintBuild", function(c)
    vim.cmd("silent! write")
    term(fl .. " build " .. file() .. " " .. c.args)
  end, { nargs = "*", desc = "Flint: build current file (args: --release, --debug, -o name)" })
  vim.api.nvim_create_user_command("FlintEmit", function()
    local out = vim.fn.systemlist(fl .. " emit " .. file())
    vim.cmd("vnew")
    vim.api.nvim_buf_set_lines(0, 0, -1, false, out)
    vim.bo.buftype, vim.bo.bufhidden, vim.bo.filetype = "nofile", "wipe", "c"
  end, { desc = "Flint: show generated C" })
  vim.api.nvim_create_user_command("FlintLint", function()
    vim.cmd("silent! write")
    local lines = vim.fn.systemlist(fl .. " lint " .. file() .. " 2>&1")
    vim.fn.setqflist({}, " ", { title = "flint lint", lines = lines, efm = "%f:%l:%c: %t%*[^:]: %m" })
    vim.cmd(#lines > 0 and "copen" or "cclose")
    if #lines == 0 then
      vim.notify("flint lint: clean")
    end
  end, { desc = "Flint: lint current file into quickfix" })

  if opts.keymaps then
    vim.api.nvim_create_autocmd("FileType", {
      group = group,
      pattern = "flint",
      callback = function(args)
        local function map(lhs, rhs, desc)
          vim.keymap.set("n", lhs, rhs, { buffer = args.buf, desc = desc })
        end
        map("<leader>cr", "<cmd>FlintRun<cr>", "Flint run")
        map("<leader>cb", "<cmd>FlintBuild<cr>", "Flint build")
        map("<leader>ce", "<cmd>FlintEmit<cr>", "Flint show C")
        map("<leader>cl", "<cmd>FlintLint<cr>", "Flint lint")
        map("gd", vim.lsp.buf.definition, "LSP go to definition")
      end,
    })
  end
end

return M

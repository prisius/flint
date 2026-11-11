" Flint indentation: one level per open bracket at end of line.
if exists("b:did_indent") | finish | endif
let b:did_indent = 1
setlocal indentexpr=FlintIndent() autoindent
setlocal indentkeys=0{,0},0),0],!^F,o,O
let b:undo_indent = "setl inde< indk< ai<"

if exists("*FlintIndent") | finish | endif
function! FlintIndent() abort
  let lnum = prevnonblank(v:lnum - 1)
  if lnum == 0
    return 0
  endif
  let prev = substitute(getline(lnum), '\s*//.*$', '', '')
  let ind = indent(lnum)
  if prev =~# '[{(\[]\s*$'
    let ind += shiftwidth()
  elseif prev =~# '\(=>\|[-+*/%&|^=<>]\)\s*$' && prev !~# '->\s*$'
    let ind += shiftwidth()          " continuation after an operator
  endif
  if getline(v:lnum) =~# '^\s*[}\])]'
    let ind -= shiftwidth()
  endif
  return ind < 0 ? 0 : ind
endfunction

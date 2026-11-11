" Syntax highlighting for the Flint language (.fl)
if exists("b:current_syntax") | finish | endif

syn keyword flintKeyword fn let var const return defer as in impl extern nextgroup=flintFuncName skipwhite
syn keyword flintStructure struct enum nextgroup=flintTypeName skipwhite
syn keyword flintInclude import cinclude link
syn keyword flintConditional if else match
syn keyword flintRepeat while for
syn keyword flintStatement break continue
syn keyword flintBoolean true false
syn keyword flintConstant null
syn keyword flintType i8 i16 i32 i64 u8 u16 u32 u64 f32 f64 bool char void str int uint float byte list
syn keyword flintSelf self
syn keyword flintBuiltin print println eprint eprintln assert panic exit free read_line parse_int parse_float time_ns read_file write_file args sizeof new make

syn match flintFuncName "\h\w*" contained
syn match flintFuncCall "\<\h\w*\ze\s*("
syn match flintTypeName "\<\u\w*\>"
syn match flintDiscard "\<_\>"

syn match flintNumber "\<\d[0-9_]*\>"
syn match flintNumber "\<0[xX][0-9a-fA-F_]\+\>"
syn match flintNumber "\<0[bB][01_]\+\>"
syn match flintFloat "\<\d[0-9_]*\.\d[0-9_]*\([eE][+-]\=\d\+\)\=\>"
syn match flintFloat "\<\d[0-9_]*[eE][+-]\=\d\+\>"

syn match flintEscape "\\[ntr0\\\"'e]" contained
syn match flintEscape "\\x\x\x" contained
syn match flintBraceEsc "{{\|}}" contained
syn region flintInterp matchgroup=flintInterpDelim start="{\ze[^{]" end="}" contained contains=flintNumber,flintFloat,flintBuiltin,flintFuncCall,flintTypeName,flintBoolean,flintSelf,flintFmtSpec
syn match flintFmtSpec ":-\=0\=\d*\(\.\d\+\)\=[xXobe]\=\ze}" contained
syn region flintString start=+"+ skip=+\\\\\|\\"+ end=+"+ oneline contains=flintEscape,flintBraceEsc,flintInterp
syn match flintChar "'\([^'\\]\|\\.\|\\x\x\x\)'" contains=flintEscape

syn match flintOperator "=>\|->\|\.\.\|[-+*/%&|^!~<>=]=\=\|&&\|||\|<<=\=\|>>=\="

syn keyword flintTodo TODO FIXME XXX NOTE HACK contained
syn region flintComment start="//" end="$" contains=flintTodo,@Spell
syn region flintComment start="/\*" end="\*/" contains=flintTodo,@Spell

hi def link flintKeyword Keyword
hi def link flintStructure Structure
hi def link flintInclude Include
hi def link flintConditional Conditional
hi def link flintRepeat Repeat
hi def link flintStatement Statement
hi def link flintBoolean Boolean
hi def link flintConstant Constant
hi def link flintType Type
hi def link flintTypeName Type
hi def link flintSelf Identifier
hi def link flintBuiltin Function
hi def link flintFuncName Function
hi def link flintFuncCall Function
hi def link flintDiscard Comment
hi def link flintNumber Number
hi def link flintFloat Float
hi def link flintString String
hi def link flintChar Character
hi def link flintEscape SpecialChar
hi def link flintBraceEsc SpecialChar
hi def link flintInterpDelim Special
hi def link flintFmtSpec SpecialChar
hi def link flintOperator Operator
hi def link flintComment Comment
hi def link flintTodo Todo

let b:current_syntax = "flint"

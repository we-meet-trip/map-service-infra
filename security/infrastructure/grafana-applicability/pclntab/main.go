// pclntab reports only actual, non-inlined Go function entries from exact ELF input.
// An absent entry never proves absence of inlined code or dynamic reachability.
package main

import (
	"bytes"
	"crypto/sha256"
	"debug/elf"
	"debug/gosym"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"sort"
)

type Function struct {
	Name string `json:"name"`
	Entry uint64 `json:"text_offset"`
	End uint64 `json:"end_text_offset"`
}
type Report struct {
	SHA256 string `json:"binary_sha256"`
	Section string `json:"section"`
	NameTable []string `json:"function_name_table_including_inlined"`
	Count int `json:"function_count"`
	Functions []Function `json:"functions"`
	Scope string `json:"scope"`
}

func inspect(path string) (*Report, error) {
	f, err := elf.Open(path)
	if err != nil { return nil, err }
	defer f.Close()
	if f.Class != elf.ELFCLASS64 || f.Data != elf.ELFDATA2LSB || f.Machine != elf.EM_X86_64 || (f.Type != elf.ET_EXEC && f.Type != elf.ET_DYN) { return nil, fmt.Errorf("unsupported target ELF") }
	text := f.Section(".text")
	section := f.Section(".gopclntab")
	if section == nil { section = f.Section(".data.rel.ro.gopclntab") }
	if text == nil || section == nil || section.Size > 128<<20 { return nil, fmt.Errorf("missing or unbounded Go tables") }
	data, err := section.Data()
	if err != nil { return nil, err }
	if len(data) < 72 || data[4] != 0 || data[5] != 0 || data[7] != 8 { return nil, fmt.Errorf("unsupported pclntab header") }
	magic := binary.LittleEndian.Uint32(data)
	if magic != 0xfffffff0 && magic != 0xfffffff1 { return nil, fmt.Errorf("unsupported pclntab version") }
	count := binary.LittleEndian.Uint64(data[8:16])
	if count == 0 || count > 1000000 { return nil, fmt.Errorf("invalid function count") }
	// Go1.26 linker marks header word2 unused (zero); do not mistake it for an
	// absolute runtime.text address. Zero base retains actual text-relative offsets.
	table, err := gosym.NewTable(nil, gosym.NewLineTable(data, 0))
	if err != nil { return nil, err }
	if uint64(len(table.Funcs)) != count { return nil, fmt.Errorf("incomplete function table") }
	result := &Report{Section: section.Name, Count: len(table.Funcs), Scope: "Actual non-inlined entries with text-relative offsets, plus compiler function-name table containing linked/inlined Go names. No call graph or exploitability proof. Target binary was not executed."}
	for _, fn := range table.Funcs {
		if fn.Name == "" || fn.End < fn.Entry || fn.End > text.Size { return nil, fmt.Errorf("invalid function entry") }
		result.Functions = append(result.Functions, Function{fn.Name, fn.Entry, fn.End})
	}
	nameStart:=binary.LittleEndian.Uint64(data[32:40])
	nameEnd:=binary.LittleEndian.Uint64(data[40:48])
	if nameStart>=nameEnd||nameEnd>uint64(len(data)) { return nil,fmt.Errorf("invalid function name table offsets") }
	for _,name:=range bytes.Split(data[nameStart:nameEnd],[]byte{0}) { if len(name)>0 { result.NameTable=append(result.NameTable,string(name)) } }
	if len(result.NameTable)<len(result.Functions) { return nil,fmt.Errorf("incomplete function name table") }
	sort.Strings(result.NameTable)
	sort.Slice(result.Functions, func(i,j int) bool { return result.Functions[i].Name < result.Functions[j].Name })
	input, err := os.Open(path)
	if err != nil { return nil, err }
	defer input.Close()
	h := sha256.New()
	if _, err := io.Copy(h,input); err != nil { return nil, err }
	result.SHA256 = hex.EncodeToString(h.Sum(nil))
	return result,nil
}

func main() {
	if len(os.Args)!=2 { fmt.Fprintln(os.Stderr,"usage: pclntab exact-linux-amd64-elf");os.Exit(2) }
	result,err:=inspect(os.Args[1])
	if err!=nil { fmt.Fprintln(os.Stderr,err);os.Exit(1) }
	if err=json.NewEncoder(os.Stdout).Encode(result);err!=nil { fmt.Fprintln(os.Stderr,err);os.Exit(1) }
}

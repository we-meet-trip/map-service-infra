package main

import (
	"debug/elf"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
)

func TestRealStrippedELFFunctionEntriesWithoutExecution(t *testing.T) {
	dir:=t.TempDir()
	source:=filepath.Join(dir,"fixture.go")
	if err:=os.WriteFile(source,[]byte("package main\nimport \"fmt\"\n//go:noinline\nfunc linkedMarker() string { return \"marker\" }\nfunc deadMarker() string { return \"dead\" }\nfunc main(){fmt.Println(linkedMarker())}\n"),0600);err!=nil { t.Fatal(err) }
	bin:=filepath.Join(dir,"stripped")
	command:=exec.Command("go","build","-trimpath","-ldflags=-s -w","-o",bin,source)
	command.Env=append(os.Environ(),"GOOS=linux","GOARCH=amd64","CGO_ENABLED=0")
	if out,err:=command.CombinedOutput();err!=nil { t.Fatalf("compile fixture: %v: %s",err,out) }
	f,err:=elf.Open(bin)
	if err!=nil { t.Fatal(err) }
	_,err=f.Symbols();f.Close()
	if err!=elf.ErrNoSymbols { t.Fatalf("fixture must be stripped, got %v",err) }
	r,err:=inspect(bin)
	if err!=nil { t.Fatal(err) }
	linked,dead:=false,false
	for _,fn:=range r.Functions { linked=linked||fn.Name=="main.linkedMarker";dead=dead||fn.Name=="main.deadMarker" }
	if !linked||dead||r.Count==0||len(r.SHA256)!=64 { t.Fatalf("unexpected extraction: linked=%v dead=%v count=%d",linked,dead,r.Count) }
}

func TestMalformedInputDoesNotBecomeEmptyCleanReport(t *testing.T) {
	p:=filepath.Join(t.TempDir(),"not-elf")
	if err:=os.WriteFile(p,[]byte("not executable"),0600);err!=nil { t.Fatal(err) }
	if _,err:=inspect(p);err==nil { t.Fatal("malformed ELF must fail") }
}

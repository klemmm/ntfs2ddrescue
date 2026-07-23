# NTFS2ddrescue

## Introduction

This tool is designed to analyze an NTFS Master File Table (MFT) and generate targeted domain mapfiles for GNU ddrescue, enabling the surgical recovery of specific files and directory structures rather than cloning entire volumes. This is meant to improve recovery odds when dealing with a dying disk by avoiding stressing the disk as much as possible.

Please be aware that interacting with failing or physically degraded storage media **carries inherent risks**; any read attempts, mechanical seeking, or prolonged operation will probably **accelerate hardware degradation**, risking **sudden and permanent drive failure** and **irreversible data loss**.

This software is provided "as-is" under the WTFPL License, without warranty of any kind, express or implied. The author assumes no liability for further data loss or hardware damage. Use these tool at your own risk. If the data is of a vital importance, the best course of action is to ask for the service of a professional data recovery company.


## Find MFT of MFTMirror offset

```
$ ./hunt_mft.py --image rescue.bin --log rescue.log -- -N -n -r1 -f /dev/sda rescue.bin rescue.log
Launching: ddrescue --mapfile-interval=1s -N -n -r1 -f /dev/sda rescue.bin rescue.log

[...]

Found a valid MFT record at offset 3222274048 (0xC0100000)

[...]

```

## Map MFT locations
```

$ ./map_mft.py --mft main --rescue-image rescue.bin --rescue-log rescue.log -o mft.map -r rescue.rec /dev/sda 0xC0100000
Reading MFT and MFTMirror entries at:  0xc0100000
Reader: Read from image
Reader: Read from image
Using MFT start offset:  0xc0100000
Cluster size is:  4096
MFT data run start is:  786432
MFT mirror data run start is:  2
Volume start (using MFT, forced by --mft main):  0x100000
Wrote ddrescue domain mapfile to mft.map
Wrote MFT record list to rescue.rec
```

## Recover MFT with ddrescue

```
$ ddrescue -m mft.map /dev/sda rescue.bin rescue.log
GNU ddrescue 1.27
Press Ctrl-C to interrupt
     ipos:  691730 MB, non-trimmed:        0 B,  current rate:   6049 kB/s
     opos:  691730 MB, non-scraped:        0 B,  average rate:   5862 kB/s
non-tried:        0 B,  bad-sector:        0 B,    error rate:       0 B/s
  rescued:   64487 kB,   bad areas:        0,        run time:         10s
pct rescued:  100.00%, read errors:        0,  remaining time:         n/a
                              time since last successful read:         n/a
Copying non-tried blocks... Pass 1 (forwards)
Finished                              
```

## Look at recovered MFT to list files


```
$ ./list_files.py -o rescue.lst rescue.bin rescue.rec
Wrote 1163 files to rescue.lst
$ cp rescue.lst selected.lst
```

## Select files to recover
```
$ nano selected.lst
```

Keep the files you want

```
$ cat selected.lst 
/video/GH013139.MP4        0x66361DE000-0x3017000
/video/GH013140.MP4        0x663339D000-0x2E41000
```

## Map selected files content

```
$ ./map_files.py -o files.map rescue.bin selected.lst
Wrote ddrescue domain mapfile to files.map: 2 file regions coalesced into 1 blocks (98926592 bytes, merge gap 262144)
```

## Recover file contents with ddrescue

```
$ ddrescue -m files.map /dev/sda rescue.bin rescue.log
GNU ddrescue 1.27
Press Ctrl-C to interrupt
Initial status (read from mapfile)
(sizes limited to domain from 428_658_292Ki B to 428_754_900Ki B of 1_953_514_584Ki B)
rescued: 0 B, tried: 0 B, bad-sector: 0 B, bad areas: 0

Current status
     ipos:  439044 MB, non-trimmed:        0 B,  current rate:  33574 kB/s
     opos:  439044 MB, non-scraped:        0 B,  average rate:  16487 kB/s
non-tried:        0 B,  bad-sector:        0 B,    error rate:       0 B/s
  rescued:   98926 kB,   bad areas:        0,        run time:          5s
pct rescued:  100.00%, read errors:        0,  remaining time:         n/a
                              time since last successful read:         n/a
Copying non-tried blocks... Pass 1 (forwards)
Finished                               
```

## Extract recovered files

```
$ ./recover_files.py rescue.bin selected.lst recovered
Recovered 2 files to recovered (0 skipped, 0 bytes unrecoverable)
user@normal:~/perso/mftrescue$ file recovered/video/GH0131*
recovered/video/GH013139.MP4: ISO Media, MP4 v1 [ISO 14496-1:ch13]
recovered/video/GH013140.MP4: ISO Media, MP4 v1 [ISO 14496-1:ch13]
```


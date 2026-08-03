import csv
import argparse

def convert_csv(input_file, output_file):
    with open(input_file, 'r') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    with open(output_file, 'w') as f:
        writer = csv.writer(f)
        writer.writerow(['secs', 'nsecs', 'offset', 'sequence', 'P_norm', 'Q_norm'])

        for i, row in enumerate(rows):
            timestep = float(row['timestep'])
            secs = int(timestep * 1)
            nsecs = int((timestep * 1 - secs) * 1e9)

            writer.writerow([
                secs,
                nsecs,
                0.0,
                i,
                row['P_norm'],
                row['Q_norm']
            ])

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Convert CSV files to VILLASnode format.')
    parser.add_argument('--inputs', nargs='+', required=True, help='List of input CSV files')
    parser.add_argument('--output_suffix', type=str, default='_converted', help='Suffix for output files (default: "_converted")')
    args = parser.parse_args()

    for input_file in args.inputs:
        output_file = input_file.replace('.csv', args.output_suffix + '.csv')
        convert_csv(input_file, output_file)
        print(f"Converted {input_file} to {output_file}")

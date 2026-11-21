import {Composition, registerRoot} from 'remotion';
import './fonts';
import {GodotSegmentPromo} from './Promo';
import {FPS} from './tokens';
import {TOTAL_FRAMES} from './timeline';

const Root: React.FC = () => (
  <>
    <Composition
      id="GodotSegmentPromo"
      component={GodotSegmentPromo}
      durationInFrames={TOTAL_FRAMES}
      fps={FPS}
      width={1920}
      height={1080}
    />
  </>
);

registerRoot(Root);
